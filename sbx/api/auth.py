"""
Who may call what (Build Plan 2f)
the admin key (SANDBOX_ADMIN_KEY in .env) may call everything
a turn token, made by the runner for one turn, may only call exec and files on that turn's sandbox
no key or an unknown key -> 401; a turn token used anywhere else -> 403
"""

from __future__ import annotations

import os
import secrets
import threading

from fastapi import Header, HTTPException


HTTP_UNAUTHORIZED = 401
HTTP_FORBIDDEN = 403


## TURN TOKENS ##

# kept in memory only: a server restart ends every turn anyway
_turn_tokens: dict[str, str] = {}   # token -> sandbox_id
_turn_tokens_guard = threading.Lock()


def new_turn_token(sandbox_id: str) -> str:
    """Makes a random token that works for this sandbox until drop_turn_token."""
    token = secrets.token_urlsafe(32)
    with _turn_tokens_guard:
        _turn_tokens[token] = sandbox_id
    return token


def drop_turn_token(token: str) -> None:
    """Stops a turn token from working; called when its turn ends."""
    with _turn_tokens_guard:
        _turn_tokens.pop(token, None)


def sandbox_for_turn_token(token: str) -> str | None:
    """Gives the sandbox a turn token belongs to, or nothing if it isn't one."""
    with _turn_tokens_guard:
        return _turn_tokens.get(token)


## THE ADMIN KEY ##

def admin_key() -> str:
    """The admin key from .env; the server won't start without it."""
    return os.environ.get("SANDBOX_ADMIN_KEY", "")


def require_admin_key_set() -> None:
    """Called at startup: refuse to run with no admin key, rather than run with no auth."""
    if not admin_key():
        raise RuntimeError("SANDBOX_ADMIN_KEY is not set; add it to .env (see .env.example)")


def is_admin(token: str) -> bool:
    """Compares in constant time, so the key can't be guessed from response times."""
    key = admin_key()
    return bool(key) and secrets.compare_digest(token.encode(), key.encode())


## CHECKS (FastAPI dependencies) ##

def bearer_token(authorization: str | None) -> str:
    """Takes the token out of 'Authorization: Bearer <token>', or answers 401."""
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=HTTP_UNAUTHORIZED, detail="Missing API key")
    return token


def require_admin(authorization: str | None = Header(default=None)) -> None:
    """For routes only the admin key may call: create, list, get, delete, messages, events."""
    token = bearer_token(authorization)

    if is_admin(token):
        return
    if sandbox_for_turn_token(token) is not None:
        raise HTTPException(status_code=HTTP_FORBIDDEN, detail="A turn token can only call exec and files")
    raise HTTPException(status_code=HTTP_UNAUTHORIZED, detail="Invalid API key")


def require_admin_or_turn(sandbox_id: str, authorization: str | None = Header(default=None)) -> None:
    """For exec and files: the admin key, or a turn token for this same sandbox."""
    token = bearer_token(authorization)

    if is_admin(token):
        return

    token_sandbox_id = sandbox_for_turn_token(token)
    if token_sandbox_id == sandbox_id:
        return
    if token_sandbox_id is not None:
        raise HTTPException(status_code=HTTP_FORBIDDEN, detail="This turn token is for another sandbox")
    raise HTTPException(status_code=HTTP_UNAUTHORIZED, detail="Invalid API key")
