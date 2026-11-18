"""The API heartbeat. One memory, a tick, then pause or resume, then a wait."""

import threading
import time
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from fastapi.testclient import TestClient

import node_agent
from api import db, helpers
from api.main import app
from api.models import EventType, SandboxState
from control import heartbeat
from control.live import Judgement, SampleMemory, judge_live, judge_sandbox
from control.types import Activity, Decision, Policy


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


def test_a_waiting_sandbox_with_no_cpu_history_is_not_paused(tmp_path, monkeypatch):
    """A question with no samples is not quiet yet, so the beat does not pause it."""
    monkeypatch.setenv("SBX_DB", str(tmp_path / "state.db"))
    db.create_tables()
    monkeypatch.setattr(node_agent, "read_cpu_usage_usec", lambda sandbox_id: None)
    db.insert_sandbox(ID)
    db.set_state(ID, SandboxState.RUNNING)
    db.append_event(ID, EventType.QUESTION, {"text": "which file?"})
    paused: list[str] = []
    monkeypatch.setattr(helpers, "pause", lambda sandbox_id: paused.append(sandbox_id))
    stop = threading.Event()
    real_judge = judge_live

    def one_pass(memory: SampleMemory, now=None, policy=None) -> list:
        found = real_judge(memory, now or datetime.now(UTC))
        stop.set()
        return found

    monkeypatch.setattr("control.live.judge_live", one_pass)
    heartbeat.run_until_stopped(SampleMemory(), stop, interval=0)

    assert paused == []
    assert db.get_sandbox(ID).state is SandboxState.RUNNING


def test_a_pause_decision_pauses_and_a_resume_decision_resumes(monkeypatch):
    """The beat calls pause or resume for the sandbox the judge named."""
    paused: list[str] = []
    resumed: list[str] = []
    monkeypatch.setattr(helpers, "pause", lambda sandbox_id: paused.append(sandbox_id))
    monkeypatch.setattr(helpers, "resume", lambda sandbox_id: resumed.append(sandbox_id))
    stop = threading.Event()
    other = "sbx_" + "b" * 32

    def one_pass(memory: SampleMemory, now=None, policy=None) -> list[Judgement]:
        stop.set()
        return [
            Judgement(ID, Activity.IDLE, Decision.PAUSE, "waiting and quiet for the policy period"),
            Judgement(other, Activity.WORKING, Decision.KEEP_RUNNING, "the agent is not waiting or idle"),
            Judgement(other, Activity.WAITING, Decision.RESUME, "a new user request arrived after the pause"),
        ]

    monkeypatch.setattr("control.live.judge_live", one_pass)
    heartbeat.run_until_stopped(SampleMemory(), stop, interval=0)

    assert paused == [ID]
    assert resumed == [other]


def test_a_pause_failure_does_not_stop_the_beat(monkeypatch):
    """One sandbox that cannot pause leaves the rest of the pass free to resume."""
    resumed: list[str] = []

    def refuse(sandbox_id: str) -> None:
        raise HTTPException(status_code=409, detail="no")

    monkeypatch.setattr(helpers, "pause", refuse)
    monkeypatch.setattr(helpers, "resume", lambda sandbox_id: resumed.append(sandbox_id))
    stop = threading.Event()
    other = "sbx_" + "b" * 32

    def one_pass(memory: SampleMemory, now=None, policy=None) -> list[Judgement]:
        stop.set()
        return [
            Judgement(ID, Activity.IDLE, Decision.PAUSE, "waiting and quiet for the policy period"),
            Judgement(other, Activity.WAITING, Decision.RESUME, "a new user request arrived after the pause"),
        ]

    monkeypatch.setattr("control.live.judge_live", one_pass)
    heartbeat.run_until_stopped(SampleMemory(), stop, interval=0)

    assert resumed == [other]


def test_an_idle_quiet_sandbox_is_paused(tmp_path, monkeypatch):
    """A finished turn and a quiet CPU history make the beat call pause. The row changes only there."""
    monkeypatch.setenv("SBX_DB", str(tmp_path / "state.db"))
    db.create_tables()
    monkeypatch.setattr(node_agent, "read_cpu_usage_usec", lambda sandbox_id: 1_000_000)
    db.insert_sandbox(ID)
    db.set_state(ID, SandboxState.RUNNING)
    db.append_event(ID, EventType.TURN_FINISHED, {"exit_code": 0})
    paused: list[str] = []

    def fake_pause(sandbox_id: str) -> None:
        paused.append(sandbox_id)
        db.set_state(sandbox_id, SandboxState.PAUSED)

    monkeypatch.setattr(helpers, "pause", fake_pause)
    memory = SampleMemory()
    start = datetime.now(UTC)
    found = None
    for step in range(8):
        found = judge_sandbox(memory, ID, start + timedelta(seconds=5 * step))

    from api.enforce import apply_judgements

    apply_judgements([found])

    assert found is not None
    assert found.activity is Activity.IDLE
    assert found.decision is Decision.PAUSE
    assert paused == [ID]
    assert db.get_sandbox(ID).state is SandboxState.PAUSED


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
