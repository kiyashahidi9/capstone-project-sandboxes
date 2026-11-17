from __future__ import annotations

from pydantic import AwareDatetime

from control.types import VmSample


# Workflow: poll reads one sample from a list of recorded VM readings.
# Past replays live in tools/replay.py. The live heartbeat is control/live.py.


def poll(samples: list[VmSample], now: AwareDatetime) -> VmSample | None:
    """The sample recorded at exactly now, or nothing if that time has no sample."""
    if now.tzinfo is None:
        raise TypeError("poll time needs a timezone")
    matched: list[VmSample] = [sample for sample in samples if sample.timestamp == now]
    if not matched:
        return None
    return matched[-1]
