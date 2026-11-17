"""Decide the sandbox's next state: PAUSE, RESUME, or KEEP_RUNNING. 
This is not the agent's next step. 
The agent's activity (WORKING, WAITING, IDLE, UNKNOWN) is an input from classify. 
A quiet WAITING or IDLE agent can pause the sandbox.
 A new user request resumes a paused sandbox. 
 This file does not call the node agent or change the sandbox itself."""

from __future__ import annotations

from datetime import timedelta

from pydantic import AwareDatetime

from control.types import Activity, AgentEvent, Decision, EventType, ObservationWindow, Policy, VmSample, VmState


PAUSABLE: set[Activity] = {Activity.WAITING, Activity.IDLE}


def decide(activity: Activity, window: ObservationWindow, policy: Policy) -> tuple[Decision, str]:
    """Return the sandbox's next state for this agent activity and window, and a short reason."""
    # Sample: (Decision.PAUSE, "waiting and quiet for 30 seconds")
    # earliest time stamp to latest is end 
    samples: list[VmSample] = sorted(window.samples, key=lambda sample: sample.timestamp)
    paused_at: AwareDatetime | None = _pause_started_at(samples)
    if paused_at is not None and _has_request_after(window.events, paused_at):
        return Decision.RESUME, "a new user request arrived after the pause"
    if paused_at is not None:
        return Decision.KEEP_RUNNING, "the sandbox is already paused"
    if activity not in PAUSABLE:
        return Decision.KEEP_RUNNING, "the agent is not waiting or idle"
        
    quiet_span: timedelta | None = _trailing_quiet_span(samples, policy)
    if quiet_span is not None and quiet_span >= policy.quiet_period:
        return Decision.PAUSE, "waiting and quiet for the policy period"
    return Decision.KEEP_RUNNING, "the quiet period is not long enough"


def _pause_started_at(samples: list[VmSample]) -> AwareDatetime | None:
    """When the current paused run began. None when the latest sample is not paused."""
    if not samples or samples[-1].vm_state is not VmState.PAUSED:
        return None
    started_at: AwareDatetime = samples[-1].timestamp
    index: int = len(samples) - 1
    while index >= 0 and samples[index].vm_state is VmState.PAUSED:
        started_at: AwareDatetime = samples[index].timestamp
        index -= 1
    return started_at


def _has_request_after(events: list[AgentEvent], instant: AwareDatetime) -> bool:
    """True when a user instruction is strictly newer than instant."""
    for event in events:
        if event.event_type is EventType.USER_INSTRUCTION and event.timestamp > instant:
            return True
    return False


def _trailing_quiet_span(samples: list[VmSample], policy: Policy) -> timedelta | None:
    """How long the newest samples have stayed under the CPU limit with no network bytes."""
    if not samples:
        return None
    start_index: int = len(samples)
    index: int = len(samples) - 1
    while index >= 0 and _is_quiet(samples[index], policy):
        start_index: int = index
        index -= 1
    if start_index == len(samples):
        return None
    return samples[-1].timestamp - samples[start_index].timestamp


def _is_quiet(sample: VmSample, policy: Policy) -> bool:
    return (
        sample.cpu_percent < policy.max_cpu_percent
        and sample.network_rx_bytes_delta == 0
        and sample.network_tx_bytes_delta == 0
    )
