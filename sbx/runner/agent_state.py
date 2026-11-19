"""Rebuild the chat from stored returns, in seq order.

This reads events the runner already saved. It does not call the model or any tool.
The rebuild itself lives in sdk/history.py, so the example agent and replay share one version of it.
"""

from __future__ import annotations

from sdk.history import chat_from_events


def messages_through(events: list[dict]) -> list[dict]:
    """The chat those events produce, in OpenAI's format (without the system prompt)."""
    return chat_from_events(events)
