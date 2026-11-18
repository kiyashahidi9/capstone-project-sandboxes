"""Read one sandbox's state, events, and current CPU, then name the judge's decision.

This does not pause or resume the VM. The API process keeps the previous CPU
reading and the recent samples. Those are lost on restart. paused_at in the
database still marks when a pause began.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import NamedTuple

from pydantic import AwareDatetime

import node_agent
from api import db
from api.models import EventType as StoredEvent
from api.models import SandboxRow, SandboxState
from control.classifier import classify
from control.decider import decide
from control.heartbeat import get_observation_window
from control.types import Activity, AgentEvent, Decision, EventType, Policy, VmSample, VmState


STORED_TO_JUDGE: dict[StoredEvent, EventType] = {
    StoredEvent.USER_MESSAGE: EventType.USER_INSTRUCTION,
    StoredEvent.TURN_STARTED: EventType.AGENT_STARTED,
    StoredEvent.QUESTION: EventType.AGENT_WAITING_FOR_USER,
    StoredEvent.TURN_FINISHED: EventType.AGENT_COMPLETED,
    StoredEvent.TURN_FAILED: EventType.AGENT_FAILED,
}


class Judgement(NamedTuple):
    """One sandbox's activity and the decision for it. The decision is not carried out."""

    sandbox_id: str
    activity: Activity
    decision: Decision
    reason: str


@dataclass
class Reading:
    """The previous cgroup counter, so the next reading can become a percent."""

    usage_usec: int
    at: AwareDatetime


@dataclass
class SampleMemory:
    """CPU history for this process. Not written to the database."""

    previous: dict[str, Reading] = field(default_factory=dict)
    samples: dict[str, list[VmSample]] = field(default_factory=dict)


def load_events(sandbox_id: str) -> list[AgentEvent]:
    """Map the stored conversation onto the events classify understands.

    agent_output is left out. It is a line the user saw, not a lifecycle event.
    """
    events: list[AgentEvent] = []
    for stored in db.list_events(sandbox_id):
        kind: EventType | None = STORED_TO_JUDGE.get(stored.type)
        if kind is None:
            continue
        events.append(AgentEvent(
            event_id=f"{sandbox_id}:{stored.seq}",
            timestamp=datetime.fromtimestamp(stored.at, timezone.utc),
            event_type=kind,
            data=stored.data,
        ))
    return events


def vm_state_of(state: SandboxState) -> VmState:
    """The judge's two VM states. Only a paused row is PAUSED."""
    if state is SandboxState.PAUSED:
        return VmState.PAUSED
    return VmState.RUNNING


def paused_anchor(row: SandboxRow, now: AwareDatetime, policy: Policy) -> VmSample | None:
    """A PAUSED sample at paused_at, pulled forward into the window when the pause is older than it."""
    if row.state is not SandboxState.PAUSED or row.paused_at is None:
        return None
    paused_at: AwareDatetime = datetime.fromtimestamp(row.paused_at, timezone.utc)
    cutoff: AwareDatetime = now - policy.window
    timestamp: AwareDatetime = paused_at
    if timestamp < cutoff:
        timestamp = cutoff
    if timestamp > now:
        timestamp = now
    return _sample(timestamp, 0.0, VmState.PAUSED)


def take_cpu_sample(memory: SampleMemory, row: SandboxRow, now: AwareDatetime) -> VmSample | None:
    """One CPU sample. The first reading has no percent yet. A paused VM still gets a PAUSED sample."""
    usage: int | None = node_agent.read_cpu_usage_usec(row.id)
    previous: Reading | None = memory.previous.get(row.id)
    percent: float | None = None
    if previous is not None and usage is not None:
        elapsed: float = (now - previous.at).total_seconds()
        percent = node_agent.cpu_percent(previous.usage_usec, usage, elapsed)
    if usage is not None:
        memory.previous[row.id] = Reading(usage_usec=usage, at=now)

    vm_state: VmState = vm_state_of(row.state)
    if vm_state is VmState.PAUSED:
        return _sample(now, 0.0 if percent is None else percent, VmState.PAUSED)
    if percent is None:
        return None
    return _sample(now, percent, VmState.RUNNING)


def samples_for(memory: SampleMemory, row: SandboxRow, now: AwareDatetime, policy: Policy) -> list[VmSample]:
    """Remember this tick's sample, then add the pause anchor for the judge."""
    kept: list[VmSample] = list(memory.samples.get(row.id, []))
    fresh: VmSample | None = take_cpu_sample(memory, row, now)
    if fresh is not None:
        kept.append(fresh)
    cutoff: AwareDatetime = now - policy.window
    kept = [sample for sample in kept if sample.timestamp >= cutoff]
    memory.samples[row.id] = kept

    anchor: VmSample | None = paused_anchor(row, now, policy)
    if anchor is None:
        return list(kept)
    return [*kept, anchor]


def judge_sandbox(
        memory: SampleMemory,
        sandbox_id: str,
        now: AwareDatetime,
        policy: Policy | None = None,
        ) -> Judgement | None:
    """Classify one sandbox and decide. None when it is missing or destroyed."""
    chosen: Policy = Policy() if policy is None else policy
    row: SandboxRow | None = db.get_sandbox(sandbox_id)
    if row is None or row.state is SandboxState.DESTROYED:
        return None
    window = get_observation_window(load_events(sandbox_id), samples_for(memory, row, now, chosen), now, chosen)
    activity: Activity
    activity, _activity_reason = classify(window)
    decision: Decision
    reason: str
    decision, reason = decide(activity, window, chosen)
    return Judgement(sandbox_id, activity, decision, reason)


def judge_live(
        memory: SampleMemory,
        now: AwareDatetime | None = None,
        policy: Policy | None = None,
        ) -> list[Judgement]:
    """Judge every sandbox that has not been destroyed. Nothing is paused or resumed."""
    instant: AwareDatetime = datetime.now(timezone.utc) if now is None else now
    chosen: Policy = Policy() if policy is None else policy
    judgements: list[Judgement] = []
    for row in db.list_live_sandboxes():
        found: Judgement | None = judge_sandbox(memory, row.id, instant, chosen)
        if found is not None:
            judgements.append(found)
    return judgements


def _sample(timestamp: AwareDatetime, cpu: float, vm_state: VmState) -> VmSample:
    """A sample with no network measurement. Both deltas are 0."""
    return VmSample(
        timestamp=timestamp,
        cpu_percent=cpu,
        network_rx_bytes_delta=0,
        network_tx_bytes_delta=0,
        vm_state=vm_state,
    )
