"""
The agent runner (Build Plan 2e, locked down in 2f)
runs the client's agent once per message, following docs/agent_contract.md
turns every line it prints into an event, and knows when the turn starts and ends
"""

## IMPORTS ##

from __future__ import annotations

import collections
import json
import os
import secrets
import shlex
import signal
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field

import locks
from api import auth, db
from api.models import EventType, SandboxState, TurnState


## CONFIG ##

DEFAULT_TURN_TIMEOUT = 1800       # seconds; override with AGENT_TURN_TIMEOUT in .env
STDERR_LINES_KEPT = 50            # how much of stderr a failed turn saves
AGENT_SANDBOX_API = "http://127.0.0.1:8000"   # where the agent reaches the API, given as SANDBOX_API
AI_KEY_NAMES = ("OPENAI_API_KEY",)            # the only secrets from .env the agent gets

# locked down (2f): the agent runs through systemd-run as this user, with these limits
AGENT_USER = "sbx-agent"
TURNS_DIR = "/var/lib/sbx-turns"  # history and env files; made by scripts/setup_agents.sh
SYSTEMD_PROPERTIES = (
    "NoNewPrivileges=yes",        # no sudo, no setuid programs
    "ProtectSystem=strict",       # the whole filesystem is read-only
    "ProtectHome=yes",            # /home and /root are hidden: the repo, .env, uv's Python
    "PrivateTmp=yes",             # its own empty /tmp
    "PrivateDevices=yes",         # no access to disks or /dev/kvm
    "MemoryMax=2G",
    "CPUQuota=100%",              # one CPU's worth
    "TasksMax=256",               # no fork bombs
    "IPAddressDeny=169.254.169.254",   # Google's metadata server, which hands out cloud credentials
)

# without lockdown (AGENT_ISOLATION=none, for tests and development only): plain Popen;
# PATH so its command can be found, SYSTEMROOT because Python on Windows won't start without it
PASSED_WITHOUT_LOCKDOWN = ("PATH", "SYSTEMROOT")


## RUNNING TURNS ##

@dataclass
class Turn:
    """One turn in progress, so stop_turn can find and kill its agent."""

    process: subprocess.Popen | None = None   # None until the agent has started
    unit: str | None = None                   # the systemd unit, when locked down
    stopped: bool = False                     # set by stop_turn
    timed_out: bool = False                   # set when the time limit kills it
    done: threading.Event = field(default_factory=threading.Event)


_turns: dict[str, Turn] = {}
_turns_guard = threading.Lock()


def locked_down() -> bool:
    """
    Whether agents run through systemd-run (the default)
    AGENT_ISOLATION=none turns it off: only for tests and development, never with real clients
    """
    return os.environ.get("AGENT_ISOLATION", "systemd").strip().lower() != "none"


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

    # the token only works for this sandbox, and only until the turn ends
    token = auth.new_turn_token(sandbox_id)
    history_path = write_history(history)
    try:
        environment = agent_environment(sandbox_id, history_path, token)
        run_agent(sandbox_id, turn, shlex.split(command), environment, message.data["text"])
    finally:
        auth.drop_turn_token(token)
        os.remove(history_path)


## FILES FOR THE AGENT ##

def turn_file(prefix: str, suffix: str) -> str:
    """
    Makes an empty file only root can read, and gives its path
    locked down: in TURNS_DIR, which the agent can enter but not list, so names can't be guessed
    """
    folder = TURNS_DIR if locked_down() else None   # None: the system temp folder
    handle, path = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=folder)
    os.close(handle)
    return path


def write_history(history: list[dict]) -> str:
    """Saves the earlier events to a JSON file the agent can read, and gives its path."""
    path = turn_file("history-", ".json")

    with open(path, "w", encoding="utf-8") as file:
        json.dump(history, file)

    # locked down: readable by the agent's group too, still not by other users
    if locked_down():
        import grp   # POSIX only

        os.chown(path, -1, grp.getgrnam(AGENT_USER).gr_gid)
        os.chmod(path, 0o640)

    return path


def agent_environment(sandbox_id: str, history_path: str, token: str) -> dict[str, str]:
    """
    The environment from the agent contract
    only the AI key from .env, so the agent never sees the admin key or anything else
    """
    environment = {name: os.environ[name] for name in AI_KEY_NAMES if name in os.environ}

    if not locked_down():
        environment.update({name: os.environ[name] for name in PASSED_WITHOUT_LOCKDOWN if name in os.environ})

    environment.update(
        SANDBOX_ID=sandbox_id,
        SANDBOX_API=AGENT_SANDBOX_API,
        SANDBOX_TOKEN=token,
        SANDBOX_HISTORY=history_path,
    )
    return environment


def write_environment_file(environment: dict[str, str]) -> str:
    """
    Saves the environment for systemd to read, root only
    keeps the AI key and token out of the command line, where `ps` would show them to every user
    """
    path = turn_file("env-", ".env")

    with open(path, "w", encoding="utf-8") as file:
        for name, value in environment.items():
            file.write(f"{name}={shlex.quote(value)}\n")

    return path


def systemd_command(unit: str, environment_file: str, agent_command: list[str]) -> list[str]:
    """
    Wraps the agent's command in systemd-run with the 2f lockdown
    --pipe keeps stdin and stdout connected, so the contract doesn't change
    """
    command = [
        "systemd-run",
        "--wait",                     # stay running until the agent exits, and exit with its code
        "--pipe",                     # connect the agent's stdin, stdout and stderr to ours
        "--collect",                  # forget the unit afterwards, even if it failed
        "--quiet",                    # no "Running as unit ..." line on stderr
        f"--unit={unit}",             # a name, so we can kill it
        f"--uid={AGENT_USER}",
        f"--gid={AGENT_USER}",
    ]

    # systemd's own limit, a little after ours, in case the runner itself hangs
    properties = list(SYSTEMD_PROPERTIES)
    properties.append(f"RuntimeMaxSec={int(turn_timeout()) + 30}")
    properties.append(f"EnvironmentFile={environment_file}")   # read by systemd as root, before switching user

    for prop in properties:
        command += ["-p", prop]

    return command + agent_command


## RUNNING THE AGENT ##

def run_agent(
        sandbox_id: str,
        turn: Turn,
        agent_command: list[str],
        environment: dict[str, str],
        message: str,
        ) -> None:
    """
    Starts the agent, records what it prints, and records how the turn ended
    kills it if it runs past AGENT_TURN_TIMEOUT
    """
    environment_file = None

    # locked down: through systemd-run; otherwise in its own process group, so killing it kills what it started
    if locked_down():
        environment_file = write_environment_file(environment)
        unit = f"sbx-turn-{sandbox_id}-{secrets.token_hex(4)}"
        command = systemd_command(unit, environment_file, agent_command)
        process_environment = None    # systemd-run itself runs with the server's environment
    else:
        unit = None
        command = agent_command
        process_environment = environment

    try:
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=process_environment,
                text=True,
                encoding="utf-8",
                errors="replace",         # a stray non-UTF-8 byte can't crash the runner
                start_new_session=True,   # POSIX only; ignored on Windows
            )
        except OSError as error:
            db.append_event(sandbox_id, EventType.TURN_FAILED, {"reason": f"agent failed to start: {error}"})
            return

        follow_agent(sandbox_id, turn, process, unit, message)

    finally:
        if environment_file is not None:
            os.remove(environment_file)


def follow_agent(sandbox_id: str, turn: Turn, process: subprocess.Popen, unit: str | None, message: str) -> None:
    """Feeds the agent its message, records its lines until it exits, then records the end."""
    # let stop_turn find it; if a pause came in while it was starting, stop it now
    with _turns_guard:
        turn.process = process
        turn.unit = unit
        stopped_already = turn.stopped
    if stopped_already:
        kill(turn)

    # keep the end of stderr in the background, so a full pipe can't block the agent
    stderr_tail: collections.deque[str] = collections.deque(maxlen=STDERR_LINES_KEPT)
    stderr_reader = threading.Thread(target=keep_lines, args=(process.stderr, stderr_tail), daemon=True)
    stderr_reader.start()

    # kill it if it runs too long
    timer = threading.Timer(turn_timeout(), time_out, args=(turn,))
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


# A JSON line with one of these types is that step. The data is the object without "type".
RECORDED_STEPS: dict[str, EventType] = {
    "llm_request": EventType.LLM_REQUEST,
    "llm_response": EventType.LLM_RESPONSE,
    "tool_started": EventType.TOOL_STARTED,
    "tool_completed": EventType.TOOL_COMPLETED,
}


def record_line(sandbox_id: str, line: str) -> None:
    """
    Saves one line the agent printed
    a JSON object with "type": "question" is a question
    a JSON object with a recorded step type is that event
    anything else is plain output
    """
    try:
        parsed = json.loads(line)
    except ValueError:
        parsed = None

    if isinstance(parsed, dict) and parsed.get("type") == "question":
        db.append_event(sandbox_id, EventType.QUESTION, {"text": str(parsed.get("text", ""))})
        return

    step: EventType | None = RECORDED_STEPS.get(parsed.get("type")) if isinstance(parsed, dict) else None
    if isinstance(parsed, dict) and step is not None:
        data: dict = {key: value for key, value in parsed.items() if key != "type"}
        db.append_event(sandbox_id, step, data)
        return

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


def time_out(turn: Turn) -> None:
    """Called by the timer when a turn runs too long."""
    turn.timed_out = True
    kill(turn)


def kill(turn: Turn) -> None:
    """Kills the agent and everything it started; harmless if it already exited."""
    process = turn.process
    if process is None:
        return

    try:
        if turn.unit is not None:
            # every process in the unit, which runs as sbx-agent; then systemd-run exits too
            subprocess.run(
                ["systemctl", "kill", "--signal=SIGKILL", turn.unit],
                capture_output=True,
                timeout=10,
            )
            process.kill()
        elif os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)   # the whole process group
        else:
            process.kill()
    except (ProcessLookupError, PermissionError, OSError, subprocess.TimeoutExpired):
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

    # not started yet: follow_agent sees stopped and kills it as soon as it starts
    kill(turn)

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
