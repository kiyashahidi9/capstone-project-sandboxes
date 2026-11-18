"""Rebuild the chat from stored returns, in seq order.

This reads events the runner already saved. It does not call the model or any tool.
"""

from __future__ import annotations

import json

from api.models import EventType


def messages_through(events: list[dict]) -> list[dict]:
    """The chat those events produce. turn_failed stops the walk."""
    messages: list[dict] = []
    for event in events:
        kind: str = str(event["type"])
        if kind == EventType.TURN_FAILED:
            break
        data: dict = event.get("data") or {}
        if kind == EventType.USER_MESSAGE:
            messages.append({"role": "user", "content": str(data.get("text", ""))})
        elif kind == EventType.LLM_RESPONSE:
            messages.append(_assistant_message(data))
        elif kind == EventType.TOOL_COMPLETED:
            messages.append(_tool_message(data))
    return messages


def _assistant_message(data: dict) -> dict:
    """One stored llm_response: its text, its tool calls, or both."""
    message: dict = {"role": "assistant"}
    if "text" in data:
        message["content"] = data["text"]
    calls: object = data.get("tool_calls")
    if calls:
        message["tool_calls"] = calls
        if "content" not in message:
            message["content"] = None
    elif "content" not in message:
        message["content"] = ""
    return message


def _tool_message(data: dict) -> dict:
    """One stored tool_completed, under the same tool_call_id the model used."""
    result: object = data.get("result", "")
    content: str = result if isinstance(result, str) else json.dumps(result)
    return {
        "role": "tool",
        "tool_call_id": str(data.get("tool_call_id", "")),
        "content": content,
    }
