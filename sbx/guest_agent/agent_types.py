"""agent_types.py - shapes of the data the guest agent sends back.

Kept separate from guest_agent.py so the API's types are in one place.
Not named types.py: that would shadow Python's own `types` module.
Standard library only, like the agent itself.
"""
from typing import TypedDict


class ExecResult(TypedDict):
    """What a foreground /exec returns (sent back as JSON)."""
    exit_code: int   # 124 if it timed out
    stdout: str
    stderr: str
    timed_out: bool


class BackgroundResult(TypedDict):
    """What a background /exec returns (sent back as JSON)."""
    pid: int   # output is in /tmp/bg-<pid>.log
