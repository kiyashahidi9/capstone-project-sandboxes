"""
One lock per sandbox, shared by the agent runner (2e) and pause/resume (4c)

TEMPORARY (4c, option b): a stand-in matching Track A's sbx/locks.py, so 4c can be built before 2e merges.
On merge, keep Track A's version; it must still give lock_for(sandbox_id) -> threading.Lock.
"""

from __future__ import annotations

import threading


_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def lock_for(sandbox_id: str) -> threading.Lock:
    """Gives the sandbox's lock, making it the first time; every caller gets the same one."""
    with _locks_guard:
        return _locks.setdefault(sandbox_id, threading.Lock())
