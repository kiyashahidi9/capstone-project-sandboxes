"""
The agent runner (Build Plan 2e)

TEMPORARY (4c, option b): only stop_turn, as a do-nothing stand-in, so pause can call it before 2e merges.
On merge, replace this whole file with Track A's runner.py; its stop_turn has the same signature.
"""

from __future__ import annotations


def stop_turn(sandbox_id: str) -> None:
    """
    Cuts off the sandbox's running turn, for pausing (4c)
    stand-in: no turns exist yet, so there is nothing to stop
    Track A's version returns once the turn has ended and the lock is free; call it before taking the lock
    """
