"""
The thin chat app end to end: client -> SDK -> API -> runner -> fake agent -> events -> client
only the VM is faked
"""

import shlex
import sys
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

import node_agent
from api import db
from api.main import app
from api.models import SandboxState
from client import client as chat
from sdk import sandbox as sdk


AGENT = """
import json, os, sys
message = sys.stdin.read()
turns = len([e for e in json.load(open(os.environ["SANDBOX_HISTORY"])) if e["type"] == "user_message"])
if message == "ask me":
    print(json.dumps({"type": "question", "text": "which port?"}))
elif message == "break":
    print("oops", file=sys.stderr)
    sys.exit(3)
else:
    print(f"[run] {message}")
    print(f"earlier messages: {turns}")
"""


@pytest.fixture
def api(tmp_path, monkeypatch):
    """The real API in-process, with fake VMs and a fake agent; the SDK talks to it."""
    monkeypatch.setenv("SBX_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("SANDBOX_ADMIN_KEY", "test-admin-key")
    monkeypatch.setenv("AGENT_ISOLATION", "none")
    db.create_tables()

    def boot(sandbox_id=None):
        with closing(db.connect()) as conn:
            conn.execute("INSERT INTO slots (n, sandbox_id) VALUES (4, ?)", (sandbox_id,))
        return node_agent.Sandbox(id=sandbox_id, n=4, ip=node_agent.guest_ip_for(4), pid=10, boot_seconds=0.1)

    def destroy(sandbox_id):
        with closing(db.connect()) as conn:
            conn.execute("DELETE FROM slots WHERE sandbox_id = ?", (sandbox_id,))

    monkeypatch.setattr(node_agent, "create_sandbox", boot)
    monkeypatch.setattr(node_agent, "destroy_sandbox", destroy)

    script = tmp_path / "agent.py"
    script.write_text(AGENT)
    monkeypatch.setenv("AGENT_CMD", shlex.join([sys.executable, str(script)]))

    with TestClient(app) as test_client:
        def request(method, url, timeout=None, **kwargs):
            return test_client.request(method, url, **kwargs)

        monkeypatch.setattr(sdk.httpx, "request", request)
        original_create = sdk.Sandbox.create.__func__
        monkeypatch.setattr(
            sdk.Sandbox,
            "create",
            classmethod(lambda cls, token=None: original_create(cls, api="http://testserver", token=token)),
        )
        monkeypatch.setattr(chat, "POLL_SECONDS", 0.05)
        yield


def chat_with(monkeypatch, capsys, lines: list[str]) -> list[str]:
    """Runs the chat app, typing the given lines, and gives back what it printed."""
    typed = iter(lines)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(typed))
    chat.main()
    return capsys.readouterr().out.splitlines()


def test_two_tasks_in_a_row_and_the_second_sees_the_first(api, monkeypatch, capsys):
    printed = chat_with(monkeypatch, capsys, ["make hello.py", "run it", "/quit"])

    sandbox_line, *conversation, destroyed_line = printed
    assert sandbox_line.startswith("sandbox sbx_") and sandbox_line.endswith(" ready")
    assert conversation == [
        "[run] make hello.py",
        "earlier messages: 0",
        "done",
        "[run] run it",
        "earlier messages: 1",
        "done",
    ]
    assert destroyed_line.endswith(" destroyed")
    assert db.list_sandboxes()[0].state == SandboxState.DESTROYED


def test_a_question_is_marked_as_waiting_on_you(api, monkeypatch, capsys):
    printed = chat_with(monkeypatch, capsys, ["ask me", "/quit"])

    assert printed[1:3] == ["[waiting on you] which port?", "done"]


def test_a_failed_turn_shows_the_reason_and_stderr(api, monkeypatch, capsys):
    printed = chat_with(monkeypatch, capsys, ["break", "/quit"])

    assert printed[1:3] == ["error: exit", "      oops"]


def test_ctrl_c_still_destroys_the_sandbox(api, monkeypatch, capsys):
    def interrupted(prompt=""):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", interrupted)
    chat.main()

    assert db.list_sandboxes()[0].state == SandboxState.DESTROYED
