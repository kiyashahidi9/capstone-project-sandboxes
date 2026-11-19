"""
Rebuilds an agent's chat from stored events, in OpenAI's chat format (docs/observability.md, "Replay")
the example agent builds its context with these same functions, and so does replay,
so what replay shows is exactly what the model was sent
"""

## IMPORTS ##

from __future__ import annotations

import json


## CONFIG ##

# the result given for a tool call whose turn died before it finished; OpenAI needs every call answered
TURN_ENDED_EARLY = "error: the turn ended before this tool finished"


## ONE MESSAGE ##

def assistant_message(response: dict) -> dict:
    """The assistant message for one llm_response's data: its text, its tool calls, or both."""
    message: dict = {"role": "assistant", "content": response.get("text")}
    calls = response.get("tool_calls") or []

    if calls:
        message["tool_calls"] = [
            {
                "id": str(call.get("tool_call_id", "")),
                "type": "function",
                "function": {"name": str(call.get("name", "")), "arguments": arguments_text(call.get("arguments"))},
            }
            for call in calls
        ]
    elif message["content"] is None:
        message["content"] = ""

    return message


def tool_message(tool_call_id: str, result: object) -> dict:
    """The tool message for one tool_completed: text as is, anything else as JSON."""
    content = result if isinstance(result, str) else json.dumps(result)
    return {"role": "tool", "tool_call_id": tool_call_id, "content": content}


def arguments_text(arguments: object) -> str:
    """Tool arguments as the JSON text OpenAI expects; text that wasn't valid JSON stays as it was."""
    return arguments if isinstance(arguments, str) else json.dumps(arguments)


## THE WHOLE CHAT ##

def chat_from_events(events: list[dict]) -> list[dict]:
    """
    The chat the model was sent, rebuilt from events (dicts with "type" and "data", oldest first)
    each user_message starts a turn
    a turn with llm_response events uses them and tool_completed; agent_output is display only
    a turn without them (an agent that doesn't print steps) uses its agent_output lines instead
    no system message: the agent adds its own
    """
    messages: list[dict] = []

    for turn in split_into_turns(events):
        messages.append({"role": "user", "content": str(turn[0]["data"].get("text", ""))})

        if any(event["type"] == "llm_response" for event in turn):
            messages += steps_to_messages(turn[1:])
        else:
            messages += plain_output_to_messages(turn[1:])

    return messages


def split_into_turns(events: list[dict]) -> list[list[dict]]:
    """Groups events by turn: each user_message starts one; events before the first message are left out."""
    turns: list[list[dict]] = []

    for event in events:
        if event["type"] == "user_message":
            turns.append([event])
        elif turns:
            turns[-1].append(event)

    return turns


def steps_to_messages(events: list[dict]) -> list[dict]:
    """
    One turn's llm_response, tool_completed and question events as messages
    a tool call left without a result gets TURN_ENDED_EARLY, so the chat stays valid
    a result for no open call is left out, since OpenAI would refuse it
    """
    messages: list[dict] = []
    open_calls: list[str] = []      # tool_call_ids still waiting for a result, in order

    def close_open_calls() -> None:
        for call_id in open_calls:
            messages.append(tool_message(call_id, TURN_ENDED_EARLY))
        open_calls.clear()

    for event in events:
        data = event.get("data") or {}

        if event["type"] == "llm_response":
            close_open_calls()
            messages.append(assistant_message(data))
            open_calls.extend(str(call.get("tool_call_id", "")) for call in data.get("tool_calls") or [])

        elif event["type"] == "tool_completed":
            call_id = str(data.get("tool_call_id", ""))
            if call_id in open_calls:
                open_calls.remove(call_id)
                messages.append(tool_message(call_id, data.get("result", "")))

        elif event["type"] == "question":
            close_open_calls()
            messages.append({"role": "assistant", "content": str(data.get("text", ""))})

    close_open_calls()
    return messages


def plain_output_to_messages(events: list[dict]) -> list[dict]:
    """One turn's agent_output and question lines, joined into one assistant message."""
    lines = [
        str((event.get("data") or {}).get("text", ""))
        for event in events
        if event["type"] in ("agent_output", "question")
    ]
    if not lines:
        return []
    return [{"role": "assistant", "content": "\n".join(lines)}]
