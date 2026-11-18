"""
One lock per sandbox, so a turn and a pause or resume never run at the same time
used by runner/runner.py (2e) and by pause and resume (Track C, 4c)
"""

import threading


## LOCKS ##

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()   # two threads asking for a new sandbox's lock at once get the same one


def lock_for(sandbox_id: str) -> threading.Lock:
    """
    Gives the sandbox's lock, making it the first time it's asked for
    a plain Lock, so the thread that takes it must be the one that releases it
    """
    with _locks_guard:
        return _locks.setdefault(sandbox_id, threading.Lock())
