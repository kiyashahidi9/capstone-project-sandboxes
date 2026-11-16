"""Crontab entry for the live heartbeat. One firing calls poll once."""

from __future__ import annotations

import sys
from datetime import datetime, timezone

from crontab import CronTab

from control.mocks import FakeNodeAgent
from control.sampler import poll
from control.types import VmSample


COMMENT = "sbx_polling_heartbeat"


def tick(provider: FakeNodeAgent) -> VmSample | None:
    """One heartbeat. The time is the current time, and the reading comes from poll."""
    return poll(provider, datetime.now(timezone.utc))


def install_polling_job(cron: CronTab | None = None) -> CronTab:
    """Add a job that runs tick. Crontab's finest step is one minute, so this is every 5 minutes."""
    if cron is None:
        cron = CronTab(user=True)
    command = f"{sys.executable} -c 'from control.polling_cron_job import tick; tick(None)'"
    job = cron.new(command=command, comment=COMMENT)
    job.minute.every(5)
    cron.write()
    return cron


if __name__ == "__main__":
    install_polling_job()
