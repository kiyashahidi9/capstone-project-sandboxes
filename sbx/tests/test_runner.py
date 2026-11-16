import shlex
import sys
import threading
import time

import pytest

import locks
from api import db
from api.models import EventType, SandboxState, TurnState
from runner import runner


SANDBOX_ID = "sbx_" + "a" * 32
WAIT = 10   # seconds a test waits for something that should take far less


## FAKE AGENTS ##

TWO_LINES = """
print("one")
print("two")
"""

FAILS = """
import sys
print("working on it")
print("something broke", file=sys.stderr)
sys.exit(1)
"""

ASKS = """
import json
print(json.dumps({"type": "question", "text": "which port?"}))
print('{"type": "note"}')
"""

SLEEPS = """
import time
print("starting", flush=True)
time.sleep(60)
"""

# prints what the contract hands it, so a test can check it
SHOWS_INPUTS = """
import json, os, sys
print("message: " + sys.stdin.read())
print("sandbox: " + os.environ["SANDBOX_ID"])
print("api: " + os.environ["SANDBOX_API"])
history = json.load(open(os.environ["SANDBOX_HISTORY"]))
print("history: " + ",".join(event["type"] for event in history))
print("secret: " + str(os.environ.get("NOT_FOR_AGENTS")))
"""


## SETUP ##

@pytest.fixture(autouse=True)
def database(tmp_path, monkeypatch):
    """A fresh state.db with one running sandbox."""
    monkeypatch.setenv("SBX_DB", str(tmp_path / "state.db"))
    db.create_tables()
    db.insert_sandbox(SANDBOX_ID)
    db.set_state(SANDBOX_ID, SandboxState.RUNNING)


def use_agent(tmp_path, monkeypatch, source: str, timeout: float = WAIT) -> None:
    """Writes a fake agent to a file and makes it the AGENT_CMD."""
    script = tmp_path / "agent.py"
    script.write_text(source)
    monkeypatch.setenv("AGENT_CMD", shlex.join([sys.executable, str(script)]))
    monkeypatch.setenv("AGENT_TURN_TIMEOUT", str(timeout))


def send(text: str = "do the thing") -> None:
    """Does what POST /messages does before it starts the runner."""
    assert db.claim_turn(SANDBOX_ID)
    db.append_event(SANDBOX_ID, EventType.USER_MESSAGE, {"text": text})


def events() -> list[tuple[str, dict]]:
    return [(event.type, event.data) for event in db.list_events(SANDBOX_ID)]


def assert_turn_over() -> None:
    """The sandbox is idle and its lock is free."""
    assert db.get_sandbox(SANDBOX_ID).turn_state == TurnState.IDLE
    lock = locks.lock_for(SANDBOX_ID)
    assert lock.acquire(blocking=False)
    lock.release()


## TURNS ##

def test_two_lines_then_exit_zero_finishes_the_turn(tmp_path, monkeypatch):
    use_agent(tmp_path, monkeypatch, TWO_LINES)
    send()

    runner.run_turn(SANDBOX_ID)

    assert events() == [
        (EventType.USER_MESSAGE, {"text": "do the thing"}),
        (EventType.TURN_STARTED, {}),
        (EventType.AGENT_OUTPUT, {"text": "one"}),
        (EventType.AGENT_OUTPUT, {"text": "two"}),
        (EventType.TURN_FINISHED, {"exit_code": 0}),
    ]
    assert_turn_over()


def test_exit_one_fails_the_turn_with_the_end_of_stderr(tmp_path, monkeypatch):
    use_agent(tmp_path, monkeypatch, FAILS)
    send()

    runner.run_turn(SANDBOX_ID)

    *_, output, (last_type, last_data) = events()
    assert output == (EventType.AGENT_OUTPUT, {"text": "working on it"})
    assert last_type == EventType.TURN_FAILED
    assert last_data == {"reason": "exit", "exit_code": 1, "stderr_tail": "something broke"}
    assert_turn_over()


def test_a_question_line_becomes_a_question_event(tmp_path, monkeypatch):
    use_agent(tmp_path, monkeypatch, ASKS)
    send()

    runner.run_turn(SANDBOX_ID)

    assert events()[2:] == [
        (EventType.QUESTION, {"text": "which port?"}),
        (EventType.AGENT_OUTPUT, {"text": '{"type": "note"}'}),   # JSON, but not a question
        (EventType.TURN_FINISHED, {"exit_code": 0}),
    ]
    assert_turn_over()


def test_an_agent_past_the_time_limit_is_killed(tmp_path, monkeypatch):
    use_agent(tmp_path, monkeypatch, SLEEPS, timeout=1)
    send()

    started = time.monotonic()
    runner.run_turn(SANDBOX_ID)

    assert time.monotonic() - started < WAIT
    last_type, last_data = events()[-1]
    assert last_type == EventType.TURN_FAILED
    assert last_data["reason"] == "timeout"
    assert_turn_over()


def test_the_agent_gets_the_message_environment_and_earlier_events(tmp_path, monkeypatch):
    use_agent(tmp_path, monkeypatch, SHOWS_INPUTS)
    monkeypatch.setenv("NOT_FOR_AGENTS", "hunter2")
    send("first")
    runner.run_turn(SANDBOX_ID)
    first_turn_length = len(events())

    send("second")
    runner.run_turn(SANDBOX_ID)

    printed = [data["text"] for kind, data in events()[first_turn_length:] if kind == EventType.AGENT_OUTPUT]
    assert printed == [
        "message: second",
        f"sandbox: {SANDBOX_ID}",
        "api: http://127.0.0.1:8000",
        # the first turn, but not the second message, which is on stdin
        "history: " + ",".join(["user_message", "turn_started"] + ["agent_output"] * 5 + ["turn_finished"]),
        "secret: None",
    ]


def test_missing_agent_cmd_fails_the_turn(monkeypatch):
    monkeypatch.delenv("AGENT_CMD", raising=False)
    send()

    runner.run_turn(SANDBOX_ID)

    assert events()[-1] == (EventType.TURN_FAILED, {"reason": "AGENT_CMD is not set"})
    assert_turn_over()


def test_an_agent_that_cannot_start_fails_the_turn(monkeypatch):
    monkeypatch.setenv("AGENT_CMD", "/no/such/agent")
    send()

    runner.run_turn(SANDBOX_ID)

    last_type, last_data = events()[-1]
    assert last_type == EventType.TURN_FAILED
    assert last_data["reason"].startswith("agent failed to start")
    assert_turn_over()


## STOPPING ##

def wait_for_output(text: str) -> None:
    deadline = time.monotonic() + WAIT
    while (EventType.AGENT_OUTPUT, {"text": text}) not in events():
        assert time.monotonic() < deadline, f"agent never printed {text!r}"
        time.sleep(0.05)


def test_stop_turn_kills_the_agent_and_frees_the_sandbox(tmp_path, monkeypatch):
    use_agent(tmp_path, monkeypatch, SLEEPS, timeout=60)
    send()
    turn_thread = threading.Thread(target=runner.run_turn, args=(SANDBOX_ID,))
    turn_thread.start()
    wait_for_output("starting")

    runner.stop_turn(SANDBOX_ID)

    # stop_turn returns only after the turn is over
    assert_turn_over()
    turn_thread.join(WAIT)
    assert not turn_thread.is_alive()
    last_type, last_data = events()[-1]
    assert last_type == EventType.TURN_FAILED
    assert last_data["reason"] == "paused"


def test_stop_turn_without_a_turn_does_nothing():
    runner.stop_turn(SANDBOX_ID)

    assert events() == []
    assert_turn_over()


## SERVER STARTUP ##

def test_startup_ends_turns_a_crash_cut_off():
    idle_id = "sbx_" + "b" * 32
    db.insert_sandbox(idle_id)
    db.set_turn_state(SANDBOX_ID, TurnState.WORKING)

    runner.recover_interrupted_turns()

    assert events() == [(EventType.TURN_FAILED, {"reason": "server restarted"})]
    assert db.get_sandbox(SANDBOX_ID).turn_state == TurnState.IDLE
    assert db.list_events(idle_id) == []
