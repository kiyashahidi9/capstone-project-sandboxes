"""Turn fetched agent events and VM samples for one sandbox into the window classify and decide read. Drops anything outside the last 10 minutes. Sorts events and samples separately. This is not a database query."""

from __future__ import annotations

from pydantic import AwareDatetime

from control.types import AgentEvent, ObservationWindow, Policy, VmSample


def get_observation_window(
    events: list[AgentEvent],
    samples: list[VmSample],
    now: AwareDatetime,
    policy: Policy,
) -> ObservationWindow:
    """Keep rows at or before now and no older than policy.window.

    Events are sorted by (timestamp, event_id). Samples are sorted by (timestamp,).
    The two lists are not merged into one sequence.
    """
    cutoff: AwareDatetime = now - policy.window
    kept_events: list[AgentEvent] = [
        event for event in events if cutoff <= event.timestamp <= now
    ]
    kept_samples: list[VmSample] = [
        sample for sample in samples if cutoff <= sample.timestamp <= now
    ]
    ordered_events: list[AgentEvent] = sorted(
        kept_events, key=lambda event: (event.timestamp, event.event_id)
    )
    ordered_samples: list[VmSample] = sorted(
        kept_samples, key=lambda sample: (sample.timestamp,)
    )
    return ObservationWindow(as_of=now, events=ordered_events, samples=ordered_samples)
