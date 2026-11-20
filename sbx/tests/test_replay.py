"""
Replay (docs/observability.md): rebuilds what the model was sent at each step, from events alone
the main test runs the real example agent through the runner, with a fake model that logs every
message list it receives, and checks replay rebuilds exactly those lists
"""

import json
import shlex
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import db, observability
from api.main import app
from api.models import EventType, SandboxState
from runner import runner


SANDBOX_ID = "sbx_" + "c" * 32
ADMIN_KEY = "test-admin-key"
SBX_DIR = Path(__file__).resolve().parent.parent
AGENT_DIR = SBX_DIR / "examples" / "agent"

# runs examples/agent/agent.py with a fake model and sandbox; the model logs each message list it gets
WRAPPED_AGENT = r'''
import json, sys, types
sys.path[:0] = [{agent_dir!r}, {sbx_dir!r}]
import agent
from openai.types.chat import ChatCompletionMessage

LOG = {log!r}


def reply(messages):
    """Step 1 of a turn calls a tool (write for 'write ...', run otherwise); step 2 answers in text."""
    last_user = max(i for i, m in enumerate(messages) if m["role"] == "user")
    steps_done = sum(1 for m in messages[last_user:] if m["role"] == "assistant")
    if steps_done > 0:
        return ChatCompletionMessage(role="assistant", content="done")

    if "write" in messages[last_user]["content"]:
        name, arguments = "write_file", {{"path": "hello.py", "content": "print('hi')"}}
    else:
        name, arguments = "run_command", {{"cmd": "python hello.py"}}
    call = {{"id": "call_" + str(len(messages)), "type": "function", "function": {{"name": name, "arguments": json.dumps(arguments)}}}}
    return ChatCompletionMessage(role="assistant", content=None, tool_calls=[call])


class Completions:
    def create(self, model, messages, tools):
        with open(LOG, "a", encoding="utf-8") as file:
            file.write(json.dumps(messages) + "\n")
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=reply(messages))])


class FakeSandbox:
    def exec(self, cmd, cwd, background):
        return {{"exit_code": 0, "stdout": "hi\n", "stderr": "", "timed_out": False}}

    def write_file(self, path, content):
        pass


agent.OpenAI = lambda: types.SimpleNamespace(chat=types.SimpleNamespace(completions=Completions()))
agent.Sandbox = types.SimpleNamespace(from_env=lambda: FakeSandbox())
sys.exit(agent.main())
'''


## SETUP ##

@pytest.fixture(autouse=True)
def database(tmp_path, monkeypatch):
    """A fresh state.db with one running sandbox."""
    monkeypatch.setenv("SBX_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("AGENT_ISOLATION", "none")
    monkeypatch.setenv("SANDBOX_ADMIN_KEY", ADMIN_KEY)
    db.create_tables()
    db.insert_sandbox(SANDBOX_ID)
    db.set_state(SANDBOX_ID, SandboxState.RUNNING)


def run_message(text: str) -> None:
    """Does what POST /messages does, then runs the turn."""
    assert db.claim_turn(SANDBOX_ID)
    db.append_event(SANDBOX_ID, EventType.USER_MESSAGE, {"text": text})
    runner.run_turn(SANDBOX_ID)


@pytest.fixture
def model_log(tmp_path, monkeypatch) -> Path:
    """Makes the wrapped example agent the AGENT_CMD; gives the file its fake model logs to."""
    log = tmp_path / "model_calls.jsonl"
    script = tmp_path / "agents" / "wrapped_agent.py"
    script.parent.mkdir()
    script.write_text(WRAPPED_AGENT.format(agent_dir=str(AGENT_DIR), sbx_dir=str(SBX_DIR), log=str(log)))
    monkeypatch.setenv("AGENT_CMD", shlex.join([sys.executable, str(script)]))
    return log


## THE REAL AGENT ##

def test_replay_rebuilds_exactly_what_the_model_was_sent(model_log):
    run_message("write hello.py")
    run_message("now run it")

    sent = [json.loads(line) for line in model_log.read_text(encoding="utf-8").splitlines()]
    events = db.list_events(SANDBOX_ID)
    steps = observability.replay_turn(events, 1) + observability.replay_turn(events, 2)

    assert len(sent) == len(steps) == 4
    for step, messages in zip(steps, sent):
        assert step.messages == messages
        assert step.matches_record is True


def test_the_second_turn_remembers_the_first_turns_tool_results(model_log):
    run_message("write hello.py")
    run_message("now run it")

    first_step_of_turn_2 = observability.replay_turn(db.list_events(SANDBOX_ID), 2)[0]

    roles = [message["role"] for message in first_step_of_turn_2.messages]
    assert roles == ["system", "user", "assistant", "tool", "assistant", "user"]
    assert first_step_of_turn_2.new_messages == 1       # just the new user message


def test_each_step_shows_the_reply_and_tool_results(model_log):
    run_message("now run it")

    first, second = observability.replay_turn(db.list_events(SANDBOX_ID), 1)

    assert first.response["tool_calls"][0]["arguments"] == {"cmd": "python hello.py"}
    [tool] = first.tools
    assert (tool.name, tool.finished) == ("run_command", True)
    assert tool.result == {"exit_code": 0, "stdout": "hi\n", "stderr": "", "timed_out": False}
    assert second.response == {"text": "done"}
    assert second.tools == []


## RECORDED EVENTS ##

def add(kind: EventType, data: dict) -> None:
    db.append_event(SANDBOX_ID, kind, data)


def test_a_record_that_differs_from_the_rebuild_is_flagged():
    add(EventType.USER_MESSAGE, {"text": "hi"})
    add(EventType.LLM_REQUEST, {"system": "s", "messages": [{"role": "user", "content": "something else"}]})

    [step] = observability.replay_turn(db.list_events(SANDBOX_ID), 1)

    assert step.matches_record is False
    assert step.messages == [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}]


def test_a_tool_that_never_finished_and_a_model_that_never_answered():
    call = {"tool_call_id": "c1", "name": "run_command", "arguments": {"cmd": "pytest"}}
    add(EventType.USER_MESSAGE, {"text": "test it"})
    add(EventType.LLM_REQUEST, {})
    add(EventType.LLM_RESPONSE, {"tool_calls": [call]})
    add(EventType.TOOL_STARTED, call)
    add(EventType.TURN_FAILED, {"reason": "exit"})

    [step] = observability.replay_turn(db.list_events(SANDBOX_ID), 1)

    assert step.matches_record is None          # the request recorded no messages
    assert step.tools[0].finished is False
    assert step.tools[0].result is None


def test_replay_endpoint(tmp_path):
    add(EventType.USER_MESSAGE, {"text": "hi"})
    add(EventType.LLM_REQUEST, {"model": "gpt-4.1", "messages": [{"role": "user", "content": "hi"}]})
    add(EventType.LLM_RESPONSE, {"text": "hello"})

    with TestClient(app, headers={"Authorization": f"Bearer {ADMIN_KEY}"}) as client:
        found = client.get(f"/sandboxes/{SANDBOX_ID}/replay", params={"turn": 1})
        missing = client.get(f"/sandboxes/{SANDBOX_ID}/replay", params={"turn": 2})

    assert found.status_code == 200
    [step] = found.json()["steps"]
    assert (step["model"], step["matches_record"], step["response"]) == ("gpt-4.1", True, {"text": "hello"})
    assert missing.status_code == 404
