import os
import shlex
import sys
import threading
import time

import pytest

import locks
from api import auth, db
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

# one model round: the request, the tool call, the action, both results, then plain text
RECORDS_STEPS = r"""
import json
print(json.dumps({
    "type": "llm_request",
    "model": "gpt-4.1",
    "system": "You are a coding agent.",
    "tools": [{"type": "function", "function": {"name": "run_command"}}],
    "messages": [{"role": "user", "content": "do the thing"}],
}))
print(json.dumps({
    "type": "llm_response",
    "tool_calls": [{
        "tool_call_id": "call_1",
        "name": "write_file",
        "arguments": {"path": "hello.py", "content": "print('hi')"},
    }],
}))
print(json.dumps({
    "type": "tool_started",
    "tool_call_id": "call_1",
    "name": "write_file",
    "arguments": {"path": "hello.py", "content": "print('hi')"},
}))
print(json.dumps({
    "type": "tool_completed",
    "tool_call_id": "call_1",
    "result": {"path": "hello.py", "content": "print('hi')"},
}))
print(json.dumps({
    "type": "llm_response",
    "tool_calls": [{
        "tool_call_id": "call_2",
        "name": "run_command",
        "arguments": {"cmd": "echo hi"},
    }],
}))
print(json.dumps({
    "type": "tool_started",
    "tool_call_id": "call_2",
    "name": "run_command",
    "arguments": {"cmd": "echo hi"},
}))
print(json.dumps({
    "type": "tool_completed",
    "tool_call_id": "call_2",
    "result": {"exit_code": 0, "stdout": "hi\n", "stderr": ""},
}))
print(json.dumps({"type": "llm_response", "text": "done"}))
print("still plain text")
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
    monkeypatch.setenv("AGENT_ISOLATION", "none")
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


def test_a_step_line_is_stored_as_its_own_event(tmp_path, monkeypatch):
    use_agent(tmp_path, monkeypatch, RECORDS_STEPS)
    send()

    runner.run_turn(SANDBOX_ID)

    assert events() == [
        (EventType.USER_MESSAGE, {"text": "do the thing"}),
        (EventType.TURN_STARTED, {}),
        (EventType.LLM_REQUEST, {
            "model": "gpt-4.1",
            "system": "You are a coding agent.",
            "tools": [{"type": "function", "function": {"name": "run_command"}}],
            "messages": [{"role": "user", "content": "do the thing"}],
        }),
        (EventType.LLM_RESPONSE, {
            "tool_calls": [{
                "tool_call_id": "call_1",
                "name": "write_file",
                "arguments": {"path": "hello.py", "content": "print('hi')"},
            }],
        }),
        (EventType.TOOL_STARTED, {
            "tool_call_id": "call_1",
            "name": "write_file",
            "arguments": {"path": "hello.py", "content": "print('hi')"},
        }),
        (EventType.TOOL_COMPLETED, {
            "tool_call_id": "call_1",
            "result": {"path": "hello.py", "content": "print('hi')"},
        }),
        (EventType.LLM_RESPONSE, {
            "tool_calls": [{
                "tool_call_id": "call_2",
                "name": "run_command",
                "arguments": {"cmd": "echo hi"},
            }],
        }),
        (EventType.TOOL_STARTED, {
            "tool_call_id": "call_2",
            "name": "run_command",
            "arguments": {"cmd": "echo hi"},
        }),
        (EventType.TOOL_COMPLETED, {
            "tool_call_id": "call_2",
            "result": {"exit_code": 0, "stdout": "hi\n", "stderr": ""},
        }),
        (EventType.LLM_RESPONSE, {"text": "done"}),
        (EventType.AGENT_OUTPUT, {"text": "still plain text"}),
        (EventType.TURN_FINISHED, {"exit_code": 0}),
    ]
    assert_turn_over()


def test_a_question_line_becomes_a_question_event(tmp_path, monkeypatch):
    use_agent(tmp_path, monkeypatch, ASKS)
    send()

    runner.run_turn(SANDBOX_ID)

    assert events()[2:] == [
        (EventType.QUESTION, {"text": "which port?"}),
        (EventType.AGENT_OUTPUT, {"text": '{"type": "note"}'}),
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


## LOCKDOWN (2f) ##

SHOWS_TOKEN = """
import os
print(os.environ["SANDBOX_TOKEN"])
print(os.environ.get("SANDBOX_ADMIN_KEY"))
"""


def test_each_turn_gets_its_own_token_that_ends_with_the_turn(tmp_path, monkeypatch):
    use_agent(tmp_path, monkeypatch, SHOWS_TOKEN)
    monkeypatch.setenv("SANDBOX_ADMIN_KEY", "the-admin-key")
    send()
    runner.run_turn(SANDBOX_ID)
    send()
    runner.run_turn(SANDBOX_ID)

    printed = [data["text"] for kind, data in events() if kind == EventType.AGENT_OUTPUT]
    first_token, first_admin, second_token, second_admin = printed
    assert first_token and second_token and first_token != second_token
    assert first_admin == second_admin == "None"
    assert auth.sandbox_for_turn_token(first_token) is None
    assert auth.sandbox_for_turn_token(second_token) is None


def test_the_systemd_command_locks_the_agent_down(monkeypatch):
    monkeypatch.setenv("AGENT_TURN_TIMEOUT", "1800")

    command = runner.systemd_command("sbx-turn-x", "/var/lib/sbx-turns/env-1.env", ["python", "/opt/agents/agent.py"])

    assert command[0] == "systemd-run"
    assert {"--wait", "--pipe", "--collect", "--uid=sbx-agent"} <= set(command)
    properties = {command[i + 1] for i, part in enumerate(command) if part == "-p"}
    assert {
        "NoNewPrivileges=yes",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "PrivateTmp=yes",
        "PrivateDevices=yes",
        "MemoryMax=2G",
        "CPUQuota=100%",
        "TasksMax=256",
        "IPAddressDeny=169.254.169.254",
        "RuntimeMaxSec=1830",
        "EnvironmentFile=/var/lib/sbx-turns/env-1.env",
    } <= properties
    assert command[-2:] == ["python", "/opt/agents/agent.py"]


def test_the_agent_runs_from_agent_dir(tmp_path, monkeypatch):
    """Checks the agent starts inside its own folder, so relative paths and `python -m` work."""
    home = tmp_path / "my-bot"
    home.mkdir()
    monkeypatch.setenv("AGENT_DIR", str(home))
    use_agent(tmp_path, monkeypatch, "import os\nprint(os.getcwd())\n")
    send()

    runner.run_turn(SANDBOX_ID)

    printed = [data["text"] for kind, data in events() if kind == EventType.AGENT_OUTPUT]
    assert [os.path.realpath(path) for path in printed] == [os.path.realpath(home)]


def test_the_systemd_command_starts_the_agent_in_agent_dir_when_set(monkeypatch):
    """Checks the locked-down command names the agent's folder only when one is set."""
    monkeypatch.setenv("AGENT_DIR", "/opt/agents/my-bot")
    with_dir = runner.systemd_command("unit", "/env", ["python", "-m", "mybot"])

    monkeypatch.delenv("AGENT_DIR")
    without_dir = runner.systemd_command("unit", "/env", ["python", "-m", "mybot"])

    assert "--working-directory=/opt/agents/my-bot" in with_dir
    assert not any(part.startswith("--working-directory") for part in without_dir)


def test_secrets_go_in_the_environment_file_not_the_command_line(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_ISOLATION", "none")
    path = runner.write_environment_file({"OPENAI_API_KEY": "sk-secret", "SANDBOX_TOKEN": "tok"})

    try:
        command = runner.systemd_command("unit", path, ["agent"])
        with open(path, encoding="utf-8") as file:
            lines = file.read().splitlines()
    finally:
        os.remove(path)

    assert lines == ["OPENAI_API_KEY=sk-secret", "SANDBOX_TOKEN=tok"]
    assert not any("sk-secret" in part or "tok" == part for part in command)


def test_lockdown_is_on_unless_turned_off(monkeypatch):
    monkeypatch.delenv("AGENT_ISOLATION", raising=False)
    assert runner.locked_down()

    monkeypatch.setenv("AGENT_ISOLATION", "none")
    assert not runner.locked_down()
