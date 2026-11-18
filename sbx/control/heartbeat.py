"""The timed look the judge reads.

poll picks the sample at one moment. get_observation_window keeps the recent
events and samples. run_until_stopped repeats that look on the sample interval.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from pydantic import AwareDatetime

from control.types import AgentEvent, ObservationWindow, Policy, VmSample

if TYPE_CHECKING:
    from control.live import SampleMemory


def poll(samples: list[VmSample], now: AwareDatetime) -> VmSample | None:
    """The sample recorded at exactly now, or nothing if that time has no sample."""
    if now.tzinfo is None:
        raise TypeError("poll time needs a timezone")
    matched: list[VmSample] = [sample for sample in samples if sample.timestamp == now]
    if not matched:
        return None
    return matched[-1]


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


def run_until_stopped(memory: SampleMemory, stop: threading.Event, interval: float | None = None) -> None:
    """Judge, then wait. The same memory is kept for the next pass. Nothing is paused."""
    from control import live

    wait: float = Policy().sample_interval.total_seconds() if interval is None else interval
    while not stop.is_set():
        live.judge_live(memory)
        if stop.is_set():
            return
        stop.wait(wait)
