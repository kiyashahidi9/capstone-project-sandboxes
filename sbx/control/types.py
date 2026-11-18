from __future__ import annotations

from datetime import timedelta
from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class Activity(StrEnum):
    """What the sandbox looks like from the latest events and metrics."""

    WORKING = "WORKING"
    WAITING = "WAITING"
    IDLE = "IDLE"
    UNKNOWN = "UNKNOWN"


class Decision(StrEnum):
    """What the sleep judge says should happen. It does not do it."""

    PAUSE = "PAUSE"
    RESUME = "RESUME"
    KEEP_RUNNING = "KEEP_RUNNING"


class VmState(StrEnum):
    """Runtime status reported with a metric sample."""

    RUNNING = "RUNNING"
    PAUSED = "PAUSED"


class EventType(StrEnum):
    """Agent lifecycle events a fixture can record."""

    USER_INSTRUCTION = "USER_INSTRUCTION"
    AGENT_STARTED = "AGENT_STARTED"
    LLM_REQUEST = "LLM_REQUEST"
    LLM_RESPONSE = "LLM_RESPONSE"
    TOOL_STARTED = "TOOL_STARTED"
    TOOL_COMPLETED = "TOOL_COMPLETED"
    HTTP_REQUEST = "HTTP_REQUEST"
    HTTP_RESPONSE = "HTTP_RESPONSE"
    AGENT_COMPLETED = "AGENT_COMPLETED"
    AGENT_FAILED = "AGENT_FAILED"
    AGENT_WAITING_FOR_USER = "AGENT_WAITING_FOR_USER"


class AgentEvent(BaseModel):
    """One recorded agent event. `data` is whatever that event type carried."""

    model_config = ConfigDict(extra="forbid")

    event_id: str
    timestamp: AwareDatetime
    event_type: EventType
    data: dict | None = None


class VmSample(BaseModel):
    """One VM measurement. Network fields are bytes since the previous sample."""

    model_config = ConfigDict(extra="forbid")

    timestamp: AwareDatetime
    cpu_percent: float
    network_rx_bytes_delta: int
    network_tx_bytes_delta: int
    vm_state: VmState


class SessionFixture(BaseModel):
    """A saved session: agent events plus VM samples, in the order they were recorded."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    agent_id: str
    sandbox_id: str
    events: list[AgentEvent]
    vm_metrics: list[VmSample]


class ObservationWindow(BaseModel):
    """Events and samples kept for one look at the sandbox, already cut to the rolling window."""

    model_config = ConfigDict(extra="forbid")

    as_of: AwareDatetime
    events: list[AgentEvent]
    samples: list[VmSample]


class Policy(BaseModel):
    """Prototype thresholds. These are assumptions, not a proof the sandbox is safe to pause."""

    model_config = ConfigDict(extra="forbid")

    sample_interval: timedelta = timedelta(seconds=5)
    window: timedelta = timedelta(minutes=10)
    quiet_period: timedelta = timedelta(seconds=30)
    max_cpu_percent: float = 2.0
    baseline_timeout: timedelta = Field(
        default=timedelta(minutes=5),
        description="Fixed inactivity timeout the replay compares against.",
    )
