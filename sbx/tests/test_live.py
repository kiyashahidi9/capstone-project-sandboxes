"""The judge reading a real sandbox row, its events, and the cgroup CPU counter."""

from datetime import datetime, timedelta, timezone

import node_agent
from api import db
from api.models import EventType, SandboxState
from control.live import SampleMemory, judge_live, judge_sandbox
from control.types import Activity, Decision, VmState


ID = "sbx_" + "a" * 32
UTC = timezone.utc


def temp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("SBX_DB", str(tmp_path / "state.db"))
    db.create_tables()


def test_a_question_is_waiting(tmp_path, monkeypatch):
    """A question with no CPU history is WAITING, and the judge does not pause it."""
    temp_db(tmp_path, monkeypatch)
    monkeypatch.setattr(node_agent, "read_cpu_usage_usec", lambda sandbox_id: None)
    db.insert_sandbox(ID)
    db.set_state(ID, SandboxState.RUNNING)
    db.append_event(ID, EventType.QUESTION, {"text": "which file?"})
    db.append_event(ID, EventType.AGENT_OUTPUT, {"text": "[write] /root/hello.py"})

    found = judge_sandbox(SampleMemory(), ID, datetime.now(UTC))

    assert found is not None
    assert found.activity is Activity.WAITING
    assert found.decision is Decision.KEEP_RUNNING


def test_a_user_message_after_pause_resumes(tmp_path, monkeypatch):
    """paused_at is the start of the pause. A later user message asks for RESUME."""
    temp_db(tmp_path, monkeypatch)
    monkeypatch.setattr(node_agent, "read_cpu_usage_usec", lambda sandbox_id: None)
    db.insert_sandbox(ID)
    db.set_state(ID, SandboxState.RUNNING)
    paused_at = int(datetime.now(UTC).timestamp()) - 60
    assert db.transition(ID, [SandboxState.RUNNING], SandboxState.PAUSED, snapshot_dir="snap", paused_at=paused_at)
    db.append_event(ID, EventType.USER_MESSAGE, {"text": "continue"})

    found = judge_sandbox(SampleMemory(), ID, datetime.now(UTC))

    assert found is not None
    assert found.decision is Decision.RESUME
    assert found.reason == "a new user request arrived after the pause"


def test_the_first_cpu_reading_is_not_a_percent(tmp_path, monkeypatch):
    """Two readings make a percent of one core. The first reading is only remembered."""
    temp_db(tmp_path, monkeypatch)
    usage = {"usec": 1_000_000}
    monkeypatch.setattr(node_agent, "read_cpu_usage_usec", lambda sandbox_id: usage["usec"])
    db.insert_sandbox(ID)
    db.set_state(ID, SandboxState.RUNNING)
    memory = SampleMemory()
    start = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)

    first = judge_sandbox(memory, ID, start)
    usage["usec"] = 1_500_000
    second = judge_sandbox(memory, ID, start + timedelta(seconds=5))

    assert first is not None and second is not None
    assert memory.samples[ID][0].timestamp == start + timedelta(seconds=5)
    assert memory.samples[ID][0].cpu_percent == 10.0
    assert memory.samples[ID][0].network_rx_bytes_delta == 0
    assert memory.samples[ID][0].network_tx_bytes_delta == 0
    assert memory.samples[ID][0].vm_state is VmState.RUNNING
    assert len(memory.samples[ID]) == 1


def test_an_open_tool_stays_working(tmp_path, monkeypatch):
    """A tool_started with no tool_completed stays WORKING, even after a question."""
    temp_db(tmp_path, monkeypatch)
    monkeypatch.setattr(node_agent, "read_cpu_usage_usec", lambda sandbox_id: None)
    db.insert_sandbox(ID)
    db.set_state(ID, SandboxState.RUNNING)
    db.append_event(ID, EventType.TOOL_STARTED, {
        "tool_call_id": "call_1",
        "name": "send_email",
        "arguments": {"to": "a@b.com"},
    })
    db.append_event(ID, EventType.QUESTION, {"text": "which file?"})

    found = judge_sandbox(SampleMemory(), ID, datetime.now(UTC))

    assert found is not None
    assert found.activity is Activity.WORKING


def test_a_destroyed_sandbox_is_skipped(tmp_path, monkeypatch):
    temp_db(tmp_path, monkeypatch)
    monkeypatch.setattr(node_agent, "read_cpu_usage_usec", lambda sandbox_id: None)
    db.insert_sandbox(ID)
    db.set_state(ID, SandboxState.DESTROYED)

    assert judge_live(SampleMemory(), datetime.now(UTC)) == []
