from __future__ import annotations

from pydantic import AwareDatetime

from control.mocks import FakeNodeAgent
from control.types import VmSample


# Workflow: poll reads one sample.
# Past replays live in tools/replay.py. The live heartbeat is polling_cron_job.py.


def poll(provider: FakeNodeAgent, now: AwareDatetime) -> VmSample | None:
    '''
    With the provider, get the sample of that machine at now.
    If the machine has no sample at that time, return None.
    If it does, return the sample from that machine.
    '''
    if now.tzinfo is None:
        raise TypeError("poll time needs a timezone")
    return provider.sample_at(now)
