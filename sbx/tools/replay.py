"""Replay one sandbox's recorded VM samples on a simulated clock. Each step calls poll. This does not classify the agent or decide the sandbox's next state."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pydantic import AwareDatetime

from control.heartbeat import poll
from control.types import VmSample


def replay(
    samples: list[VmSample],
    start: AwareDatetime,
    end: AwareDatetime,
    interval: timedelta,
) -> list[VmSample | None]:
    """Walk the past from start to end, stepping by interval, and keep each poll result."""
    if interval is None:
        raise ValueError("interval is None")
    if not isinstance(interval, timedelta):
        raise ValueError("interval is not a timedelta")
    if interval < timedelta(0):
        raise ValueError("interval is negative")
    if interval == timedelta(0):
        raise ValueError("interval is zero")

    now: datetime = datetime.now(timezone.utc)
    if end > now:
        raise ValueError("end is in the future")
    if start > end:
        raise ValueError("start is after end")

    readings: list[VmSample | None] = []
    cursor: AwareDatetime = start
    while cursor <= end:
        readings.append(poll(samples, cursor))
        cursor: AwareDatetime = cursor + interval
    return readings
