"""Carry out a pause or resume the judge already named.

The routes stay the way a person asks. The beat is the automatic caller.
A failure on one sandbox does not stop the pass.
"""

from __future__ import annotations

from collections.abc import Callable

from api import helpers
from control.live import Judgement
from control.types import Decision


def apply_judgements(judgements: list[Judgement]) -> None:
    """Pause or resume each sandbox the judge named. Leave the rest alone."""
    for judgement in judgements:
        if judgement.decision is Decision.PAUSE:
            _carry_out(helpers.pause, judgement.sandbox_id)
        elif judgement.decision is Decision.RESUME:
            _carry_out(helpers.resume, judgement.sandbox_id)


def _carry_out(action: Callable[[str], object], sandbox_id: str) -> None:
    """Run one pause or resume. A sandbox that cannot change is skipped."""
    try:
        action(sandbox_id)
    except Exception:
        return
