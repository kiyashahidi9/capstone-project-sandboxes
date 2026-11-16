"""
The agent runner (Build Plan 2e)
runs the client's agent once per message, following docs/agent_contract.md
turns every line it prints into an event, and knows when the turn starts and ends
"""

## IMPORTS ##

from __future__ import annotations

import collections
import json
import os
import shlex
import signal
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field

import locks
from api import db
from api.models import EventType, SandboxState, TurnState


## CONFIG ##

DEFAULT_TURN_TIMEOUT = 1800       # seconds; override with AGENT_TURN_TIMEOUT in .env
STDERR_LINES_KEPT = 50            # how much of stderr a failed turn saves
AGENT_SANDBOX_API = "http://127.0.0.1:8000"   # where the agent reaches the API, given as SANDBOX_API

# the only host variables the agent gets; PATH so its command can be found,
# SYSTEMROOT because Python on Windows won't start without it (tests)
PASSED_TO_AGENT = ("PATH", "SYSTEMROOT", "OPENAI_API_KEY")


## RUNNING TURNS ##

@dataclass
class Turn:
    """One turn in progress, so stop_turn can find and kill its agent."""

    process: subprocess.Popen | None = None   # None until the agent has started
    stopped: bool = False                     # set by stop_turn
    timed_out: bool = False                   # set when the time limit kills it
    done: threading.Event = field(default_factory=threading.Event)


_turns: dict[str, Turn] = {}
_turns_guard = threading.Lock()


## STARTING A TURN ##

def run_turn(sandbox_id: str) -> None:
    """
    Runs one turn of the agent on the sandbox's newest message
    called in a background thread by POST /sandboxes/{id}/messages, which already set turn_state to working
    always leaves the sandbox idle and its lock free when it returns
    """
    turn = Turn()
    with _turns_guard:
        _turns[sandbox_id] = turn

    try:
        with locks.lock_for(sandbox_id):
            try:
                run_locked_turn(sandbox_id, turn)
            except Exception as error:
                # a bug in the runner must not leave the sandbox working forever
                db.append_event(sandbox_id, EventType.TURN_FAILED, {"reason": f"runner error: {error}"})
            finally:
                db.set_turn_state(sandbox_id, TurnState.IDLE)
    finally:
        # the next message may already have started a new turn; only remove this one
        with _turns_guard:
            if _turns.get(sandbox_id) is turn:
                del _turns[sandbox_id]
        turn.done.set()


def run_locked_turn(sandbox_id: str, turn: Turn) -> None:
    """Does the turn's work; run_turn holds the sandbox's lock and cleans up afterwards."""
    db.append_event(sandbox_id, EventType.TURN_STARTED, {})

    # a pause can land between the message and here
    row = db.get_sandbox(sandbox_id)
    if turn.stopped or row is None or row.state != SandboxState.RUNNING:
        db.append_event(sandbox_id, EventType.TURN_FAILED, {"reason": "paused"})
        return

    command = os.environ.get("AGENT_CMD", "").strip()
    if not command:
        db.append_event(sandbox_id, EventType.TURN_FAILED, {"reason": "AGENT_CMD is not set"})
        return

    # the newest message goes on stdin; everything before it is the history
    events = db.list_events(sandbox_id)
    message = next(event for event in reversed(events) if event.type == EventType.USER_MESSAGE)
    history = [event.model_dump() for event in events if event.seq < message.seq]

    history_path = write_history(history)
    try:
        run_agent(sandbox_id, turn, command, message.data["text"], history_path)
    finally:
        os.remove(history_path)


def write_history(history: list[dict]) -> str:
    """Saves the earlier events to a temporary JSON file and gives its path."""
    with tempfile.NamedTemporaryFile(
        "w",
        prefix="sbx-history-",
        suffix=".json",
        delete=False,     # the agent reads it after we close it; run_locked_turn deletes it
        encoding="utf-8",
    ) as file:
        json.dump(history, file)
        return file.name


def agent_environment(sandbox_id: str, history_path: str) -> dict[str, str]:
    """
    The environment from the agent contract
    only the listed host variables, so the agent never sees the rest of .env
    """
    environment = {name: os.environ[name] for name in PASSED_TO_AGENT if name in os.environ}
    environment.update(
        SANDBOX_ID=sandbox_id,
        SANDBOX_API=AGENT_SANDBOX_API,
        SANDBOX_TOKEN="",               # per-turn tokens come in 2f
        SANDBOX_HISTORY=history_path,
    )
    return environment


## RUNNING THE AGENT ##

def run_agent(sandbox_id: str, turn: Turn, command: str, message: str, history_path: str) -> None:
    """
    Starts the agent, records what it prints, and records how the turn ended
    kills it if it runs past AGENT_TURN_TIMEOUT
    """
    # start it in its own process group, so killing it also kills anything it started
    try:
        process = subprocess.Popen(
            shlex.split(command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=agent_environment(sandbox_id, history_path),
            text=True,
            encoding="utf-8",
            errors="replace",         # a stray non-UTF-8 byte can't crash the runner
            start_new_session=True,   # POSIX only; ignored on Windows
        )
    except OSError as error:
        db.append_event(sandbox_id, EventType.TURN_FAILED, {"reason": f"agent failed to start: {error}"})
        return

    # let stop_turn find it; if a pause came in while it was starting, stop it now
    with _turns_guard:
        turn.process = process
        stopped_already = turn.stopped
    if stopped_already:
        kill(process)

    # keep the end of stderr in the background, so a full pipe can't block the agent
    stderr_tail: collections.deque[str] = collections.deque(maxlen=STDERR_LINES_KEPT)
    stderr_reader = threading.Thread(target=keep_lines, args=(process.stderr, stderr_tail), daemon=True)
    stderr_reader.start()

    # kill it if it runs too long
    timer = threading.Timer(turn_timeout(), time_out, args=(turn, process))
    timer.daemon = True
    timer.start()

    send_message(process, message)

    # every line on stdout is an event
    for line in process.stdout:
        record_line(sandbox_id, line.rstrip("\r\n"))

    exit_code = process.wait()
    timer.cancel()
    stderr_reader.join()

    record_end(sandbox_id, turn, exit_code, "\n".join(stderr_tail))


def send_message(process: subprocess.Popen, message: str) -> None:
    """Writes the message to the agent's stdin and closes it, so the agent sees the end."""
    try:
        process.stdin.write(message)
        process.stdin.close()
    except OSError:
        pass   # the agent exited without reading it; its exit code tells the story


def keep_lines(stream, tail: collections.deque[str]) -> None:
    """Reads a stream to the end, keeping only its last lines."""
    for line in stream:
        tail.append(line.rstrip("\r\n"))


def record_line(sandbox_id: str, line: str) -> None:
    """
    Saves one line the agent printed
    a JSON object with "type": "question" is a question; anything else is plain output
    """
    try:
        parsed = json.loads(line)
    except ValueError:
        parsed = None

    if isinstance(parsed, dict) and parsed.get("type") == "question":
        db.append_event(sandbox_id, EventType.QUESTION, {"text": str(parsed.get("text", ""))})
    else:
        db.append_event(sandbox_id, EventType.AGENT_OUTPUT, {"text": line})


def record_end(sandbox_id: str, turn: Turn, exit_code: int, stderr_tail: str) -> None:
    """Saves turn_finished for a clean exit, or turn_failed with why it failed."""
    if turn.stopped:
        reason = "paused"
    elif turn.timed_out:
        reason = "timeout"
    elif exit_code == 0:
        db.append_event(sandbox_id, EventType.TURN_FINISHED, {"exit_code": 0})
        return
    else:
        reason = "exit"

    db.append_event(
        sandbox_id,
        EventType.TURN_FAILED,
        {"reason": reason, "exit_code": exit_code, "stderr_tail": stderr_tail},
    )


## STOPPING ##

def turn_timeout() -> float:
    """Seconds a turn may run, from AGENT_TURN_TIMEOUT (default 30 minutes)."""
    return float(os.environ.get("AGENT_TURN_TIMEOUT", DEFAULT_TURN_TIMEOUT))


def time_out(turn: Turn, process: subprocess.Popen) -> None:
    """Called by the timer when a turn runs too long."""
    turn.timed_out = True
    kill(process)


def kill(process: subprocess.Popen) -> None:
    """Kills the agent and everything it started; harmless if it already exited."""
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)   # the whole process group
        else:
            process.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass


def stop_turn(sandbox_id: str) -> None:
    """
    Cuts off the sandbox's running turn, for pausing (4c)
    kills the agent; the turn ends with turn_failed, reason "paused"
    returns once the sandbox is idle and its lock is free; does nothing if no turn is running
    call it before taking the sandbox's lock, or it waits forever
    """
    with _turns_guard:
        turn = _turns.get(sandbox_id)
        if turn is None:
            return
        turn.stopped = True
        process = turn.process

    # not started yet: run_agent sees stopped and kills it as soon as it starts
    if process is not None:
        kill(process)

    turn.done.wait()


## SERVER STARTUP ##

def recover_interrupted_turns() -> None:
    """
    Ends the turns a crash or restart cut off
    any sandbox still marked working has no agent anymore: record turn_failed and set it idle
    """
    for row in db.list_sandboxes():
        if row.turn_state == TurnState.WORKING:
            db.append_event(row.id, EventType.TURN_FAILED, {"reason": "server restarted"})
            db.set_turn_state(row.id, TurnState.IDLE)
