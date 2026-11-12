from __future__ import annotations

import httpx
from fastapi import HTTPException

import node_agent
from api import db
from api.models import (
    CreateSandboxResponse,
    ExecuteRequest,
    ExecuteResponse,
    ExecuteBackgroundResponse,
    SandboxInfo,
    SandboxRow,
    SandboxState,
)


HTTP_NOT_FOUND = 404
HTTP_CONFLICT = 409
HTTP_SERVER_ERROR = 500
HTTP_BAD_GATEWAY = 502
HTTP_SERVICE_UNAVAILABLE = 503
HTTP_CREATED = 201


# ---------------------------------------------------------------------------
# Lookups that stop a request early
# ---------------------------------------------------------------------------

def require_sandbox(sandbox_id: str) -> SandboxRow:
    """Finds a sandbox's record, or answers 404 if there is no such sandbox."""
    row = db.get_sandbox(sandbox_id)
    if row is None:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="Sandbox not found")
    return row


def require_running(sandbox_id: str) -> SandboxRow:
    """Finds a sandbox's record, or answers 409 if it isn't running yet or anymore."""
    row = require_sandbox(sandbox_id)
    if row.state != SandboxState.RUNNING:
        raise HTTPException(status_code=HTTP_CONFLICT, detail="Sandbox is not running")
    return row


# ---------------------------------------------------------------------------
# Creating a sandbox
# ---------------------------------------------------------------------------

def boot_sandbox(sandbox_id: str) -> node_agent.Sandbox:
    """Starts the VM for a recorded sandbox, or marks it destroyed and answers 503 (host full) or 500."""
    try:
        return node_agent.create_sandbox(sandbox_id)
    except node_agent.NoFreeSlotsError as error:
        db.set_state(sandbox_id, SandboxState.DESTROYED)
        raise HTTPException(status_code=HTTP_SERVICE_UNAVAILABLE, detail="No sandbox slots available") from error
    except Exception as error:
        db.set_state(sandbox_id, SandboxState.DESTROYED)
        raise HTTPException(status_code=HTTP_SERVER_ERROR, detail="Sandbox failed to start") from error


def build_create_response(sandbox_id: str, boot_seconds: float) -> CreateSandboxResponse:
    """Describes a freshly booted sandbox to the client, including its boot time."""
    row = db.get_sandbox(sandbox_id)
    if row is None:
        raise RuntimeError(f"sandbox {sandbox_id} disappeared after boot")
    info = SandboxInfo.from_row(row)
    return CreateSandboxResponse(
        id=info.id,
        state=info.state,
        ip=info.ip,
        created_at=info.created_at,
        updated_at=info.updated_at,
        boot_seconds=boot_seconds,
    )


# ---------------------------------------------------------------------------
# Destroying a sandbox
# ---------------------------------------------------------------------------

def is_destroyed(row: SandboxRow) -> bool:
    """Checks whether a sandbox has already been torn down."""
    return row.state == SandboxState.DESTROYED


def tear_down(sandbox_id: str) -> None:
    """Removes a sandbox from the host, then records it as destroyed."""
    node_agent.destroy_sandbox(sandbox_id)
    db.set_state(sandbox_id, SandboxState.DESTROYED)


# ---------------------------------------------------------------------------
# Talking to the guest agent
# ---------------------------------------------------------------------------

def guest_error_to_http(error: httpx.HTTPStatusError) -> HTTPException:
    """Turns a guest agent error into one for the client: its 4xx passes through, anything else is 502."""
    status = error.response.status_code
    if not 400 <= status < 500:
        status = HTTP_BAD_GATEWAY
    detail = "Guest agent failed"
    try:
        body = error.response.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and isinstance(body.get("error"), str):
        detail = body["error"]
    return HTTPException(status_code=status, detail=detail)


def run_command(sandbox_id: str, request: ExecuteRequest) -> dict:
    """Runs a command inside the sandbox and gives back the guest agent's raw answer."""
    try:
        return node_agent.exec(
            sandbox_id,
            request.cmd,
            cwd=request.cwd,
            background=request.background,
            timeout=request.timeout,
        )
    except httpx.HTTPStatusError as error:
        raise guest_error_to_http(error) from error
    except httpx.RequestError as error:
        raise HTTPException(status_code=HTTP_BAD_GATEWAY, detail="Guest agent failed") from error


def to_execute_response(result: dict) -> ExecuteResponse:
    """Shapes a finished command's output for the client."""
    return ExecuteResponse.model_validate(result)


def to_background_response(result: dict) -> ExecuteBackgroundResponse:
    """Shapes a background command's process number for the client."""
    return ExecuteBackgroundResponse.model_validate(result)


def fetch_file(sandbox_id: str, path: str) -> str:
    """Reads a file from inside the sandbox, or answers 404 if it isn't there."""
    try:
        return node_agent.read_file(sandbox_id, path)
    except FileNotFoundError as error:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail=f"no such file: {path}") from error
    except httpx.HTTPStatusError as error:
        raise guest_error_to_http(error) from error
    except httpx.RequestError as error:
        raise HTTPException(status_code=HTTP_BAD_GATEWAY, detail="Guest agent failed") from error


def text_from_body(raw: bytes) -> str:
    """Reads an uploaded file as text, or answers 400 if the bytes are not text."""
    try:
        return raw.decode()
    except UnicodeDecodeError as error:
        raise HTTPException(status_code=400, detail="File body must be text") from error


def store_file(sandbox_id: str, path: str, content: str) -> None:
    """Saves text to a file inside a running sandbox."""
    try:
        node_agent.write_file(sandbox_id, path, content)
    except httpx.HTTPStatusError as error:
        raise guest_error_to_http(error) from error
    except httpx.RequestError as error:
        raise HTTPException(status_code=HTTP_BAD_GATEWAY, detail="Guest agent failed") from error
