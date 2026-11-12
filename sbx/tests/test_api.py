"""Sandbox routes with a fake boot. No VM and no root."""

import re
from contextlib import closing

import httpx
import pytest
from fastapi.testclient import TestClient

import node_agent
from api import db
from api.main import app
from api.models import CreateSandboxResponse, SandboxState


ID_PATTERN = re.compile(r"sbx_[0-9a-f]{32}")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SBX_DB", str(tmp_path / "state.db"))
    db.create_tables()
    with TestClient(app) as test_client:
        yield test_client


def remember_slot(sandbox_id: str, n: int) -> None:
    """Records the slot a fake boot would have claimed."""
    with closing(db.connect()) as conn:
        conn.execute("INSERT INTO slots (n, sandbox_id) VALUES (?, ?)", (n, sandbox_id))


def test_create_returns_a_running_sandbox(client, monkeypatch):
    def boot(sandbox_id: str | None = None) -> node_agent.Sandbox:
        remember_slot(sandbox_id, 4)
        return node_agent.Sandbox(
            id=sandbox_id,
            n=4,
            ip=node_agent.guest_ip_for(4),
            pid=10,
            boot_seconds=1.25,
        )

    monkeypatch.setattr(node_agent, "create_sandbox", boot)
    response = client.post("/sandboxes")

    assert response.status_code == 201
    created = CreateSandboxResponse.model_validate(response.json())
    assert ID_PATTERN.fullmatch(created.id)
    assert created.state == SandboxState.RUNNING
    assert created.ip == "10.201.0.4"
    assert created.boot_seconds == 1.25
    row = db.get_sandbox(created.id)
    assert row is not None
    assert row.state == SandboxState.RUNNING
    assert row.n == 4


def test_two_creates_get_different_ids(client, monkeypatch):
    made = {"n": 0}

    def boot(sandbox_id: str | None = None) -> node_agent.Sandbox:
        made["n"] += 1
        n = made["n"]
        remember_slot(sandbox_id, n)
        return node_agent.Sandbox(
            id=sandbox_id,
            n=n,
            ip=node_agent.guest_ip_for(n),
            pid=100 + n,
            boot_seconds=0.5,
        )

    monkeypatch.setattr(node_agent, "create_sandbox", boot)
    first = client.post("/sandboxes")
    second = client.post("/sandboxes")

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] != second.json()["id"]


def test_failed_boot_marks_the_row_destroyed(client, monkeypatch):
    def boom(sandbox_id: str | None = None) -> node_agent.Sandbox:
        raise RuntimeError("boot failed")

    monkeypatch.setattr(node_agent, "create_sandbox", boom)
    response = client.post("/sandboxes")

    assert response.status_code == 500
    assert response.json()["detail"] == "Sandbox failed to start"
    rows = db.list_sandboxes()
    assert len(rows) == 1
    assert rows[0].state == SandboxState.DESTROYED
    assert rows[0].n is None


def boot_into_slot(sandbox_id: str | None, n: int) -> node_agent.Sandbox:
    """Pretends a boot claimed slot n and came up healthy."""
    remember_slot(sandbox_id, n)
    return node_agent.Sandbox(
        id=sandbox_id,
        n=n,
        ip=node_agent.guest_ip_for(n),
        pid=10 + n,
        boot_seconds=1.25,
    )


def test_list_is_empty_before_any_sandbox(client):
    response = client.get("/sandboxes")

    assert response.status_code == 200
    assert response.json() == {"sandboxes": []}


def test_list_shows_a_running_sandbox(client, monkeypatch):
    monkeypatch.setattr(node_agent, "create_sandbox", lambda sandbox_id=None: boot_into_slot(sandbox_id, 4))
    created = client.post("/sandboxes").json()

    response = client.get("/sandboxes")

    assert response.status_code == 200
    assert response.json() == {
        "sandboxes": [
            {
                "id": created["id"],
                "state": "running",
                "ip": "10.201.0.4",
                "created_at": created["created_at"],
                "updated_at": created["updated_at"],
            }
        ]
    }


def test_list_keeps_destroyed_rows_oldest_first(client, monkeypatch):
    times = iter([1_000, 1_000, 2_000, 2_000])
    monkeypatch.setattr(db, "now", lambda: next(times))

    def boot(sandbox_id: str | None = None) -> node_agent.Sandbox:
        return boot_into_slot(sandbox_id, 4)

    def boom(sandbox_id: str | None = None) -> node_agent.Sandbox:
        raise RuntimeError("boot failed")

    monkeypatch.setattr(node_agent, "create_sandbox", boot)
    first = client.post("/sandboxes")
    monkeypatch.setattr(node_agent, "create_sandbox", boom)
    second = client.post("/sandboxes")

    response = client.get("/sandboxes")
    sandboxes = response.json()["sandboxes"]
    destroyed_id = next(row.id for row in db.list_sandboxes() if row.state == SandboxState.DESTROYED)

    assert first.status_code == 201
    assert second.status_code == 500
    assert response.status_code == 200
    assert [item["id"] for item in sandboxes] == [first.json()["id"], destroyed_id]
    assert sandboxes[0]["state"] == "running"
    assert sandboxes[0]["ip"] == "10.201.0.4"
    assert sandboxes[0]["created_at"] == 1_000
    assert sandboxes[1]["state"] == "destroyed"
    assert sandboxes[1]["ip"] is None
    assert sandboxes[1]["created_at"] == 2_000
    assert "n" not in sandboxes[0]
    assert "boot_seconds" not in sandboxes[0]


def test_get_returns_one_running_sandbox(client, monkeypatch):
    monkeypatch.setattr(node_agent, "create_sandbox", lambda sandbox_id=None: boot_into_slot(sandbox_id, 4))
    created = client.post("/sandboxes").json()

    response = client.get(f"/sandboxes/{created['id']}")

    assert response.status_code == 200
    assert response.json() == {
        "id": created["id"],
        "state": "running",
        "ip": "10.201.0.4",
        "created_at": created["created_at"],
        "updated_at": created["updated_at"],
    }


def test_get_unknown_id_is_not_found(client):
    response = client.get("/sandboxes/unknown")

    assert response.status_code == 404
    assert response.json() == {"detail": "Sandbox not found"}


def test_get_destroyed_sandbox_is_still_found(client, monkeypatch):
    def boom(sandbox_id: str | None = None) -> node_agent.Sandbox:
        raise RuntimeError("boot failed")

    monkeypatch.setattr(node_agent, "create_sandbox", boom)
    client.post("/sandboxes")
    sandbox_id = db.list_sandboxes()[0].id

    response = client.get(f"/sandboxes/{sandbox_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == sandbox_id
    assert body["state"] == "destroyed"
    assert body["ip"] is None


def fake_destroy(calls: list[str]):
    """Pretends the node agent stopped the VM and freed its slot."""
    def destroy(sandbox_id: str) -> None:
        calls.append(sandbox_id)
        with closing(db.connect()) as conn:
            conn.execute("DELETE FROM slots WHERE sandbox_id = ?", (sandbox_id,))
    return destroy


def test_delete_tears_down_a_running_sandbox_once(client, monkeypatch):
    monkeypatch.setattr(node_agent, "create_sandbox", lambda sandbox_id=None: boot_into_slot(sandbox_id, 4))
    calls: list[str] = []
    monkeypatch.setattr(node_agent, "destroy_sandbox", fake_destroy(calls))
    created = client.post("/sandboxes").json()

    first = client.delete(f"/sandboxes/{created['id']}")
    second = client.delete(f"/sandboxes/{created['id']}")
    row = db.get_sandbox(created["id"])

    assert first.status_code == 200
    assert first.json() == {"ok": True}
    assert second.status_code == 200
    assert second.json() == {"ok": True}
    assert calls == [created["id"]]
    assert row is not None
    assert row.state == SandboxState.DESTROYED
    assert row.n is None
    fetched = client.get(f"/sandboxes/{created['id']}")
    assert fetched.json()["state"] == "destroyed"
    assert fetched.json()["ip"] is None


def test_delete_unknown_id_is_not_found(client, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(node_agent, "destroy_sandbox", fake_destroy(calls))

    response = client.delete("/sandboxes/unknown")

    assert response.status_code == 404
    assert response.json() == {"detail": "Sandbox not found"}
    assert calls == []


def test_delete_skips_a_sandbox_that_is_already_destroyed(client, monkeypatch):
    def boom(sandbox_id: str | None = None) -> node_agent.Sandbox:
        raise RuntimeError("boot failed")

    monkeypatch.setattr(node_agent, "create_sandbox", boom)
    calls: list[str] = []
    monkeypatch.setattr(node_agent, "destroy_sandbox", fake_destroy(calls))
    client.post("/sandboxes")
    sandbox_id = db.list_sandboxes()[0].id

    response = client.delete(f"/sandboxes/{sandbox_id}")

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert calls == []
    assert db.get_sandbox(sandbox_id).state == SandboxState.DESTROYED


def test_delete_tears_down_a_sandbox_that_is_still_starting(client, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(node_agent, "destroy_sandbox", fake_destroy(calls))
    sandbox_id = "sbx_" + "a" * 32
    db.insert_sandbox(sandbox_id)

    response = client.delete(f"/sandboxes/{sandbox_id}")

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert calls == [sandbox_id]
    assert db.get_sandbox(sandbox_id).state == SandboxState.DESTROYED


def make_running_sandbox(client, monkeypatch) -> str:
    """Creates one running sandbox through the API and returns its id."""
    monkeypatch.setattr(node_agent, "create_sandbox", lambda sandbox_id=None: boot_into_slot(sandbox_id, 4))
    return client.post("/sandboxes").json()["id"]


def scripted_exec(calls: list[dict], result: dict):
    """Pretends the guest agent ran a command and records what it was asked."""
    def run(sandbox_id: str, cmd: str, cwd: str = "/root", background: bool = False, timeout: int = 600) -> dict:
        calls.append({
            "sandbox_id": sandbox_id,
            "cmd": cmd,
            "cwd": cwd,
            "background": background,
            "timeout": timeout,
        })
        return result
    return run


def guest_http_error(status: int, message: str) -> httpx.HTTPStatusError:
    """Builds the error httpx raises when the guest agent answers with an HTTP error."""
    request = httpx.Request("POST", "http://guest/exec")
    response = httpx.Response(status, json={"error": message}, request=request)
    return httpx.HTTPStatusError(message, request=request, response=response)


def test_exec_returns_a_finished_command(client, monkeypatch):
    sandbox_id = make_running_sandbox(client, monkeypatch)
    calls: list[dict] = []
    monkeypatch.setattr(node_agent, "exec", scripted_exec(calls, {
        "exit_code": 0,
        "stdout": "Linux\n",
        "stderr": "",
        "timed_out": False,
    }))

    response = client.post(f"/sandboxes/{sandbox_id}/exec", json={"cmd": "uname -a"})

    assert response.status_code == 200
    assert response.json() == {
        "exit_code": 0,
        "stdout": "Linux\n",
        "stderr": "",
        "timed_out": False,
    }
    assert calls == [{
        "sandbox_id": sandbox_id,
        "cmd": "uname -a",
        "cwd": "/root",
        "background": False,
        "timeout": 600,
    }]


def test_exec_returns_a_pid_for_a_background_command(client, monkeypatch):
    sandbox_id = make_running_sandbox(client, monkeypatch)
    calls: list[dict] = []
    monkeypatch.setattr(node_agent, "exec", scripted_exec(calls, {"pid": 481}))

    response = client.post(
        f"/sandboxes/{sandbox_id}/exec",
        json={"cmd": "npm start", "background": True},
    )

    assert response.status_code == 200
    assert response.json() == {"pid": 481}
    assert calls[0]["background"] is True
    assert calls[0]["cmd"] == "npm start"


def test_exec_reports_a_command_timeout_in_the_body(client, monkeypatch):
    sandbox_id = make_running_sandbox(client, monkeypatch)
    monkeypatch.setattr(node_agent, "exec", scripted_exec([], {
        "exit_code": 124,
        "stdout": "",
        "stderr": "",
        "timed_out": True,
    }))

    response = client.post(
        f"/sandboxes/{sandbox_id}/exec",
        json={"cmd": "sleep 999", "timeout": 1},
    )

    assert response.status_code == 200
    assert response.json()["exit_code"] == 124
    assert response.json()["timed_out"] is True


def test_exec_unknown_id_is_not_found(client, monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(node_agent, "exec", scripted_exec(calls, {}))

    response = client.post("/sandboxes/unknown/exec", json={"cmd": "true"})

    assert response.status_code == 404
    assert response.json() == {"detail": "Sandbox not found"}
    assert calls == []


def test_exec_rejects_a_sandbox_that_is_not_running(client, monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(node_agent, "exec", scripted_exec(calls, {}))
    starting_id = "sbx_" + "a" * 32
    db.insert_sandbox(starting_id)

    def boom(sandbox_id: str | None = None) -> node_agent.Sandbox:
        raise RuntimeError("boot failed")

    monkeypatch.setattr(node_agent, "create_sandbox", boom)
    client.post("/sandboxes")
    destroyed_id = next(row.id for row in db.list_sandboxes() if row.state == SandboxState.DESTROYED)

    starting = client.post(f"/sandboxes/{starting_id}/exec", json={"cmd": "true"})
    destroyed = client.post(f"/sandboxes/{destroyed_id}/exec", json={"cmd": "true"})

    assert starting.status_code == 409
    assert destroyed.status_code == 409
    assert starting.json() == {"detail": "Sandbox is not running"}
    assert destroyed.json() == {"detail": "Sandbox is not running"}
    assert calls == []


def test_exec_rejects_a_request_that_breaks_the_type(client, monkeypatch):
    sandbox_id = make_running_sandbox(client, monkeypatch)
    calls: list[dict] = []
    monkeypatch.setattr(node_agent, "exec", scripted_exec(calls, {}))

    empty_cmd = client.post(f"/sandboxes/{sandbox_id}/exec", json={"cmd": ""})
    huge_timeout = client.post(
        f"/sandboxes/{sandbox_id}/exec",
        json={"cmd": "true", "timeout": 3601},
    )

    assert empty_cmd.status_code == 422
    assert huge_timeout.status_code == 422
    assert calls == []


def test_exec_passes_a_guest_client_error_through(client, monkeypatch):
    sandbox_id = make_running_sandbox(client, monkeypatch)

    def reject(*args, **kwargs):
        raise guest_http_error(400, "cwd does not exist: /missing")

    monkeypatch.setattr(node_agent, "exec", reject)
    response = client.post(
        f"/sandboxes/{sandbox_id}/exec",
        json={"cmd": "true", "cwd": "/missing"},
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "cwd does not exist: /missing"}


def test_exec_turns_a_dead_guest_into_bad_gateway(client, monkeypatch):
    sandbox_id = make_running_sandbox(client, monkeypatch)

    def down(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(node_agent, "exec", down)
    response = client.post(f"/sandboxes/{sandbox_id}/exec", json={"cmd": "true"})

    assert response.status_code == 502
    assert response.json() == {"detail": "Guest agent failed"}
