from __future__ import annotations

from api import db
from api.models import EventType, TurnState


## TURNS ##

def run_turn(sandbox_id: str) -> None:
    """
    Runs one turn of the client's agent for this sandbox
    called in a background thread by POST /sandboxes/{id}/messages, after it claimed the turn
    placeholder until 2e: fails the turn right away so the sandbox doesn't stay working
    """
    db.append_event(sandbox_id, EventType.TURN_FAILED, {"reason": "agent runner not built yet"})
    db.set_turn_state(sandbox_id, TurnState.IDLE)
