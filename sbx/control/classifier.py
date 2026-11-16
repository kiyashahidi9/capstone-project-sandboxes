"""Name the agent's activity for one window, and why: WORKING, WAITING, IDLE, or UNKNOWN. 
An open tool call stays WORKING. Its id comes from the payload, 
or the event id when the payload has none. This file does not pause or resume."""

from __future__ import annotations

from control.types import Activity, AgentEvent, EventType, ObservationWindow


# A new lifecycle event is one row here. Trace events such as HTTP and LLM are absent on purpose.
LIFECYCLE: dict[EventType, Activity] = {
    EventType.USER_INSTRUCTION: Activity.WORKING,
    EventType.AGENT_STARTED: Activity.WORKING,
    EventType.AGENT_WAITING_FOR_USER: Activity.WAITING,
    EventType.AGENT_COMPLETED: Activity.IDLE,
    EventType.AGENT_FAILED: Activity.IDLE,
}

REASONS: dict[EventType, str] = {
    EventType.USER_INSTRUCTION: "a user instruction has no later completion",
    EventType.AGENT_STARTED: "the agent turn has not finished",
    EventType.AGENT_WAITING_FOR_USER: "the agent is waiting for the user",
    EventType.AGENT_COMPLETED: "the agent turn finished",
    EventType.AGENT_FAILED: "the agent turn failed",
}

def classify(window: ObservationWindow) -> tuple[Activity, str]:
    """Return the activity of this window and a short reason."""
    # Sample: (Activity.WAITING, "the agent is waiting for the user")
    events: list[AgentEvent] = list[AgentEvent](window.events)
    if not events:
        return Activity.UNKNOWN, "no agent events"

    open_tools: set[str] = _open_tool_ids(events)
    if open_tools:
        return Activity.WORKING, "a tool call is still open"

    latest: EventType | None = None
    for event in events:
        if event.event_type in LIFECYCLE:
            latest: EventType | None = event.event_type
    if latest is None:
        return Activity.UNKNOWN, "events do not say whether the turn finished"
    return LIFECYCLE[latest], REASONS[latest]


def _open_tool_ids(events: list[AgentEvent]) -> set[str]:
    """Tool ids that started and have no completion with the same id."""
    open_ids: set[str] = set()
    for event in events:
        if event.event_type is EventType.TOOL_STARTED:
            open_ids.add(_tool_id(event))
        elif event.event_type is EventType.TOOL_COMPLETED:
            open_ids.discard(_tool_id(event))
    return open_ids


def _tool_id(event: AgentEvent) -> str:
    """The tool call id on the event, or the event id when the payload has none."""
    data: dict | None = event.data
    if data is None:
        return event.event_id
    tool_id: object = data.get("tool_call_id", event.event_id)
    return str(tool_id)
