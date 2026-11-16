from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict

from control.types import AgentEvent, SessionFixture, VmSample


class ExecutorCall(BaseModel):
    """One pause or resume the fake node agent was asked to perform."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["pause", "resume"]
    sandbox_id: str
    at: AwareDatetime


class FakeEventStore:
    """Holds recorded agent events and hands back the ones up to a simulated time."""

    def __init__(self, events: list[AgentEvent]) -> None:
        self.events = list(events)

    @classmethod
    def from_session(cls, session: SessionFixture) -> FakeEventStore:
        return cls(session.events)

    def events_through(self, now: AwareDatetime) -> list[AgentEvent]:
        """Events whose timestamp is at or before `now`, in fixture order."""
        return [event for event in self.events if event.timestamp <= now]


class FakeNodeAgent:
    """Holds recorded VM samples and a list of pause/resume calls. No network, no VM."""

    def __init__(self, samples: list[VmSample], sandbox_id: str | None = None) -> None:
        self.samples = list(samples)
        self.sandbox_id = sandbox_id
        self.calls: list[ExecutorCall] = []

    @classmethod
    def from_session(cls, session: SessionFixture) -> FakeNodeAgent:
        return cls(session.vm_metrics, sandbox_id=session.sandbox_id)

    def sample_at(self, now: AwareDatetime) -> VmSample | None:
        """The sample recorded at exactly `now`, or nothing if that time has no sample."""
        matched = [sample for sample in self.samples if sample.timestamp == now]
        if not matched:
            return None
        return matched[-1]

    def pause(self, sandbox_id: str, at: AwareDatetime) -> None:
        """Record a pause. Does not change a VM."""
        self.calls.append(ExecutorCall(action="pause", sandbox_id=sandbox_id, at=at))

    def resume(self, sandbox_id: str, at: AwareDatetime) -> None:
        """Record a resume. Does not change a VM."""
        self.calls.append(ExecutorCall(action="resume", sandbox_id=sandbox_id, at=at))
