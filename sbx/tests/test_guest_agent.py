import os
import sys
import threading
from http.server import ThreadingHTTPServer

import httpx
import pytest

# the guest agent runs as a plain script inside the VM, importing its own helpers.py
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "guest_agent"))
import guest_agent  # noqa: E402


@pytest.fixture
def agent():
    """Start the real guest agent on a free local port and return a client for it."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), guest_agent.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}") as client:
        yield client
    server.shutdown()
    server.server_close()


@pytest.fixture
def system_calls(monkeypatch):
    """Record os.sync and clock changes instead of making them, so tests never touch this machine."""
    calls = []
    monkeypatch.setattr(guest_agent.os, "sync", lambda: calls.append("sync"))
    monkeypatch.setattr(guest_agent.time, "clock_settime_ns",
                        lambda clock, unix_ns: calls.append(("clock", clock, unix_ns)), raising=False)
    return calls


def test_sync_writes_cached_files_to_disk(agent, system_calls):
    response = agent.post("/sync")
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert system_calls == ["sync"]


def test_clock_sets_the_vm_clock_to_the_time_sent(agent, system_calls):
    response = agent.post("/clock", json={"unix_ns": 1_794_960_000_000_000_000})
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert system_calls == [("clock", guest_agent.time.CLOCK_REALTIME, 1_794_960_000_000_000_000)]


@pytest.mark.parametrize("body", [
    {}, {"unix_ns": "1794960000"}, {"unix_ns": 1794960000.5}, {"unix_ns": True}, {"unix_ns": 0}, {"unix_ns": -1},
], ids=["missing", "string", "float", "bool", "zero", "negative"])
def test_clock_rejects_a_bad_time(agent, system_calls, body):
    response = agent.post("/clock", json=body)
    assert response.status_code == 400
    assert system_calls == []
