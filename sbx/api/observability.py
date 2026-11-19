"""
Observability (docs/observability.md)
groups a sandbox's events into turns and sums each one up, so a client can see how a turn went
and what the agent was doing when it failed
reads stored events only: nothing here records anything
"""

## IMPORTS ##

from __future__ import annotations

from api.models import Event, EventType, FailureCause, ReplayStep, ReplayTool, TurnStatus, TurnSummary
from sdk.history import chat_from_events


## CONFIG ##

UNRESPONSIVE_AFTER = 300    # seconds with no new event before a working turn counts as unresponsive

END_TYPES = (EventType.TURN_FINISHED, EventType.TURN_FAILED)

# what the agent did, as opposed to turn bookkeeping or pauses
ACTION_TYPES = (
    EventType.AGENT_OUTPUT,
    EventType.QUESTION,
    EventType.LLM_REQUEST,
    EventType.LLM_RESPONSE,
    EventType.TOOL_STARTED,
    EventType.TOOL_COMPLETED,
)

# turn_failed reasons that are our side's fault; the runner writes them without a cause
HOST_REASONS = ("server restarted", "runner error", "agent failed to start", "AGENT_CMD is not set")


## TURNS ##

def split_into_turns(events: list[Event]) -> list[list[Event]]:
    """
    Groups events by turn: each user_message starts a new turn
    events before the first message belong to no turn and are left out
    """
    turns: list[list[Event]] = []

    for event in events:
        if event.type == EventType.USER_MESSAGE:
            turns.append([event])
        elif turns:
            turns[-1].append(event)

    return turns


def events_in_turn(events: list[Event], turn: int) -> list[Event] | None:
    """One turn's events (turns count from 1), or None if there is no such turn."""
    turns = split_into_turns(events)
    if 1 <= turn <= len(turns):
        return turns[turn - 1]
    return None


def summarize_turns(events: list[Event], agent_working: bool, now: int) -> list[TurnSummary]:
    """One summary per turn, oldest first; only the newest turn can still be running."""
    turns = split_into_turns(events)
    return [
        summarize_turn(number, turn_events, agent_working and number == len(turns), now)
        for number, turn_events in enumerate(turns, start=1)
    ]


## ONE TURN ##

def summarize_turn(number: int, events: list[Event], working: bool, now: int) -> TurnSummary:
    """Sums up one turn: how it ended, why, and the agent's last action."""
    end = next((event for event in events if event.type in END_TYPES), None)
    end_data = end.data if end is not None else {}
    last_event = events[-1]

    # a pause or turn end can come after the last action, so look only before the end
    before_end = events if end is None else [event for event in events if event.seq < end.seq]
    actions = [event for event in before_end if event.type in ACTION_TYPES]

    return TurnSummary(
        turn=number,
        message=str(events[0].data.get("text", "")),    # a turn always starts with its user_message
        status=turn_status(end, working, now - last_event.at),
        reason=end_data.get("reason"),
        cause=failure_cause(end),
        exit_code=end_data.get("exit_code"),
        stderr_tail=end_data.get("stderr_tail"),
        started_at=events[0].at,
        ended_at=end.at if end is not None else None,
        first_seq=events[0].seq,
        last_seq=last_event.seq,
        seconds_since_last_event=max(0, now - last_event.at),
        last_action=actions[-1] if actions else None,
        unfinished_step=unfinished_step(before_end),
    )


def turn_status(end: Event | None, working: bool, quiet_seconds: int) -> TurnStatus:
    """Finished, failed or paused from the end event; otherwise running, unresponsive or incomplete."""
    if end is not None:
        if end.type == EventType.TURN_FINISHED:
            return TurnStatus.FINISHED
        if end.data.get("reason") == "paused":
            return TurnStatus.PAUSED
        return TurnStatus.FAILED

    if not working:
        return TurnStatus.INCOMPLETE
    if quiet_seconds >= UNRESPONSIVE_AFTER:
        return TurnStatus.UNRESPONSIVE
    return TurnStatus.RUNNING


def failure_cause(end: Event | None) -> FailureCause | None:
    """
    Whose fault a failed turn was
    the runner stores the cause for exit and timeout, after checking the VM
    the other reasons tell the cause on their own
    """
    if end is None or end.type != EventType.TURN_FAILED:
        return None

    stored = end.data.get("cause")
    if stored in {cause.value for cause in FailureCause}:
        return FailureCause(stored)

    reason = str(end.data.get("reason", ""))
    if reason == "paused":
        return FailureCause.PAUSED
    if reason.startswith(HOST_REASONS):
        return FailureCause.HOST
    return None


def unfinished_step(events: list[Event]) -> Event | None:
    """
    The newest step that never got its answer
    a tool_started with no tool_completed for the same tool_call_id,
    or an llm_request with no llm_response after it
    """
    open_tools: dict[str, Event] = {}
    open_request: Event | None = None

    for event in events:
        call_id = str(event.data.get("tool_call_id", ""))

        if event.type == EventType.TOOL_STARTED:
            open_tools[call_id] = event
        elif event.type == EventType.TOOL_COMPLETED:
            open_tools.pop(call_id, None)
        elif event.type == EventType.LLM_REQUEST:
            open_request = event
        elif event.type == EventType.LLM_RESPONSE:
            open_request = None

    unanswered = list(open_tools.values())
    if open_request is not None:
        unanswered.append(open_request)

    if not unanswered:
        return None
    return max(unanswered, key=lambda event: event.seq)


## REPLAY ##

def replay_turn(events: list[Event], turn: int) -> list[ReplayStep] | None:
    """
    Every model call in a turn: what the model was sent, what it answered, and what its tools returned
    rebuilt from events with sdk/history.py, the same code the example agent builds its chat with
    calls nothing; None if there is no such turn
    """
    turn_events = events_in_turn(events, turn)
    if turn_events is None:
        return None

    all_events = [event.model_dump() for event in events]
    history_length = len(chat_from_events([event for event in all_events if event["seq"] < turn_events[0].seq]))
    requests = [event for event in turn_events if event.type == EventType.LLM_REQUEST]

    steps: list[ReplayStep] = []
    shown_before = None     # how many messages the previous step had

    for number, request in enumerate(requests, start=1):
        next_seq = requests[number].seq if number < len(requests) else None
        after_request = [
            event for event in turn_events
            if event.seq > request.seq and (next_seq is None or event.seq < next_seq)
        ]

        # what the model was sent: the agent's system prompt, then the chat rebuilt from every earlier event
        rebuilt = chat_from_events([event for event in all_events if event["seq"] < request.seq])
        system = request.data.get("system")
        messages = ([{"role": "system", "content": system}] if system is not None else []) + rebuilt

        recorded = request.data.get("messages")
        response = next((event for event in after_request if event.type == EventType.LLM_RESPONSE), None)

        if shown_before is None:
            new_messages = len(rebuilt) - history_length
        else:
            new_messages = len(messages) - shown_before
        shown_before = len(messages)

        steps.append(ReplayStep(
            step=number,
            seq=request.seq,
            at=request.at,
            model=request.data.get("model"),
            messages=messages,
            new_messages=new_messages,
            matches_record=None if recorded is None else recorded == rebuilt,
            response=response.data if response is not None else None,
            tools=replay_tools(response, after_request),
        ))

    return steps


def replay_tools(response: Event | None, after_request: list[Event]) -> list[ReplayTool]:
    """The tool calls one model reply asked for, each with its result if it finished."""
    calls = (response.data.get("tool_calls") or []) if response is not None else []
    results = {
        str(event.data.get("tool_call_id", "")): event.data.get("result")
        for event in after_request
        if event.type == EventType.TOOL_COMPLETED
    }

    tools: list[ReplayTool] = []
    for call in calls:
        call_id = str(call.get("tool_call_id", ""))
        tools.append(ReplayTool(
            tool_call_id=call_id,
            name=call.get("name"),
            arguments=call.get("arguments"),
            result=results.get(call_id),
            finished=call_id in results,
        ))
    return tools
