"""One pass of the live heartbeat. The API process calls this. It does not install a crontab."""

from __future__ import annotations

from pydantic import AwareDatetime

from control.live import Judgement, SampleMemory, judge_live


def tick(memory: SampleMemory, now: AwareDatetime | None = None) -> list[Judgement]:
    """Judge every live sandbox once. Each item is sandbox id, activity, decision, and reason."""
    return judge_live(memory, now)
