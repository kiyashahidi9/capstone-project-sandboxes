"""The API heartbeat. One memory, a tick, then a wait. It does not pause or resume."""

import threading
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import node_agent
from api import db
from api.main import app
from api.models import EventType, SandboxState
from control import heartbeat
from control.live import SampleMemory, judge_live
from control.types import Policy


ID = "sbx_" + "a" * 32
UTC = timezone.utc


def test_the_beat_reuses_one_memory_and_waits(monkeypatch):
    """Two ticks see the same memory. The wait is the policy's sample interval."""
    seen: list[SampleMemory] = []
    waits: list[float] = []
    stop = threading.Event()

    def fake_judge(memory: SampleMemory, now=None, policy=None) -> list:
        seen.append(memory)
        if len(seen) == 2:
            stop.set()
        return []

    def fake_wait(seconds: float) -> bool:
        waits.append(seconds)
        return stop.is_set()

    monkeypatch.setattr("control.live.judge_live", fake_judge)
    monkeypatch.setattr(stop, "wait", fake_wait)
    memory = SampleMemory()

    heartbeat.run_until_stopped(memory, stop, interval=Policy().sample_interval.total_seconds())

    assert seen[0] is memory
    assert seen[1] is memory
    assert waits == [5.0]


def test_the_beat_does_not_pause_a_sandbox(tmp_path, monkeypatch):
    """A tick can say PAUSE later. This loop does not call pause."""
    monkeypatch.setenv("SBX_DB", str(tmp_path / "state.db"))
    db.create_tables()
    monkeypatch.setattr(node_agent, "read_cpu_usage_usec", lambda sandbox_id: None)
    db.insert_sandbox(ID)
    db.set_state(ID, SandboxState.RUNNING)
    db.append_event(ID, EventType.QUESTION, {"text": "which file?"})

    from api import helpers

    monkeypatch.setattr(helpers, "pause", lambda sandbox_id: pytest.fail("the beat must not pause"))
    stop = threading.Event()
    real_judge = judge_live

    def one_pass(memory: SampleMemory, now=None, policy=None) -> list:
        found = real_judge(memory, now or datetime.now(UTC))
        stop.set()
        return found

    monkeypatch.setattr("control.live.judge_live", one_pass)
    heartbeat.run_until_stopped(SampleMemory(), stop, interval=0)


def test_the_api_starts_the_beat_and_stops_it(tmp_path, monkeypatch):
    """Opening the API starts the beat. Closing it stops the thread."""
    monkeypatch.setenv("SBX_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("SANDBOX_ADMIN_KEY", "test-admin-key")
    calls: list[SampleMemory] = []

    def fake_judge(memory: SampleMemory, now=None, policy=None) -> list:
        calls.append(memory)
        return []

    monkeypatch.setattr("control.live.judge_live", fake_judge)

    with TestClient(app):
        deadline = time.monotonic() + 1
        while not calls and time.monotonic() < deadline:
            time.sleep(0.01)
        assert app.state.beat_thread.is_alive()
        assert calls
        assert calls[0] is app.state.beat_memory

    assert not app.state.beat_thread.is_alive()
