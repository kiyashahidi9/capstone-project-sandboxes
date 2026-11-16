from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pydantic import AwareDatetime

from control.mocks import FakeNodeAgent
from control.types import VmSample


# Workflow: poll reads one sample. replay calls it for past timestamps.
# The live heartbeat is the cron job in polling_cron_job.py, which calls poll through tick.


def poll(provider: FakeNodeAgent, now: AwareDatetime) -> VmSample | None:
    '''
    With the provider, get the sample of that machine at now.
    If the machine has no sample at that time, return None.
    If it does, return the sample from that machine.
    '''
    if now.tzinfo is None:
        raise TypeError("poll time needs a timezone")
    return provider.sample_at(now)


def replay(
    provider: FakeNodeAgent,
    start: AwareDatetime,
    end: AwareDatetime,
    interval: timedelta,
) -> list[VmSample | None]:
    '''
    Walk the past from start to end, stepping by interval.
    At each past time, call poll with the provider.
    Keep every result, including None when that time has no sample.
    
    Edge cases:
    if end > now raise Error("end is in the future")
    if start > end raise Error("start is after end")
    if interval is negative raise Error("interval is negative")
    if interval is zero raise Error("interval is zero")
    if interval is None raise Error("interval is None")
    if interval is not a timedelta raise Error("interval is not a timedelta")
    if interval is not a timedelta raise Error("interval is not a timedelta")
    '''
    if interval is None:
        raise ValueError("interval is None")
    if not isinstance(interval, timedelta):
        raise ValueError("interval is not a timedelta")
    if interval < timedelta(0):
        raise ValueError("interval is negative")
    if interval == timedelta(0):
        raise ValueError("interval is zero")

    now = datetime.now(timezone.utc)
    if end > now:
        raise ValueError("end is in the future")
    if start > end:
        raise ValueError("start is after end")

    readings: list[VmSample | None] = []
    cursor: AwareDatetime = start
    while cursor <= end:
        readings.append(poll(provider, cursor))
        cursor += interval
    return readings
