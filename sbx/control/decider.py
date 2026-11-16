"""Choose PAUSE, RESUME, or KEEP_RUNNING from an agent activity and its window. 
A quiet WAITING or IDLE agent can pause. A new user request wakes a paused sandbox. 
This file does not call the node agent."""

from __future__ import annotations

from control.types import Activity, Decision, ObservationWindow, Policy


def decide(activity: Activity, window: ObservationWindow, policy: Policy) -> tuple[Decision, str]:
    """Return the decision for this activity and window, and a short reason."""
    # Sample: (Decision.PAUSE, "waiting and quiet for 30 seconds")
    ...
