from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import httpx
from fastapi import HTTPException

import locks
import node_agent
from api import db
from runner import runner
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

LOCK_RETRY_SECONDS = 1.0          # how often pause and delete cut off a turn that took the lock first

# In these states a pause or resume may be under way, so a request waits for the lock to see how it ends.
# Any other state gets its answer at once, instead of waiting behind a turn that can run for 30 minutes.
WAIT_FOR_LOCK_STATES = (SandboxState.PAUSED, SandboxState.PAUSING, SandboxState.RESUMING)
PAUSABLE_STATES = (SandboxState.RUNNING, SandboxState.PAUSING, SandboxState.RESUMING)

NOT_FOUND = "Sandbox not found"
NOT_RUNNING = "Sandbox is not running"
NOT_PAUSED = "Sandbox is not paused"
PAUSE_FAILED = "Sandbox failed to pause"
RESUME_FAILED = "Sandbox failed to resume"


# ---------------------------------------------------------------------------
# Lookups that stop a request early
# ---------------------------------------------------------------------------

def require_sandbox(sandbox_id: str) -> SandboxRow:
    """Finds a sandbox's record, or answers 404 if there is no such sandbox."""
    row = db.get_sandbox(sandbox_id)
    if row is None:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail=NOT_FOUND)
    return row


def conflict(detail: str) -> HTTPException:
    """Builds the 409 answer for a sandbox in the wrong state for a request."""
    return HTTPException(status_code=HTTP_CONFLICT, detail=detail)


def require_state_in(row: SandboxRow, states: tuple[SandboxState, ...], detail: str) -> None:
    """Answers 409 with the given message unless the sandbox is in one of the given states."""
    if row.state not in states:
        raise conflict(detail)


# ---------------------------------------------------------------------------
# The sandbox's lock (shared with the agent runner, 2e)
# ---------------------------------------------------------------------------

@contextmanager
def lock_taken_from_agent(sandbox_id: str) -> Iterator[None]:
    """Holds the sandbox's lock for a block of code, cutting off the agent's turn to get it."""
    lock = locks.lock_for(sandbox_id)
    runner.stop_turn(sandbox_id)       # before waiting: a turn holds the lock until it ends
    while not lock.acquire(timeout=LOCK_RETRY_SECONDS):
        runner.stop_turn(sandbox_id)   # a new turn took the lock first; cut that one off too
    try:
        yield
    finally:
        lock.release()


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


def destroy(sandbox_id: str) -> None:
    """Shuts a sandbox down for good from any state, waiting for a pause or resume to finish first."""
    if is_destroyed(require_sandbox(sandbox_id)):
        return
    with lock_taken_from_agent(sandbox_id):
        if not is_destroyed(require_sandbox(sandbox_id)):   # another delete may have won while we waited
            tear_down(sandbox_id)


# ---------------------------------------------------------------------------
# Pausing
# ---------------------------------------------------------------------------

def pause(sandbox_id: str) -> SandboxRow:
    """Saves a running sandbox to a snapshot, cutting off the agent's turn; answers 409 unless it's running."""
    require_state_in(require_sandbox(sandbox_id), PAUSABLE_STATES, NOT_RUNNING)
    with lock_taken_from_agent(sandbox_id):
        if not db.transition(sandbox_id, [SandboxState.RUNNING], SandboxState.PAUSING):
            raise conflict(NOT_RUNNING)
        save_snapshot(sandbox_id)
    return require_sandbox(sandbox_id)


def save_snapshot(sandbox_id: str) -> None:
    """Writes the VM to snapshot files; if that fails, records where it really ended up and answers 500."""
    try:
        node_agent.pause_sandbox(sandbox_id)
    except Exception as error:
        settle_pausing(sandbox_id)
        raise HTTPException(status_code=HTTP_SERVER_ERROR, detail=PAUSE_FAILED) from error
    mark_paused(sandbox_id)


def mark_paused(sandbox_id: str) -> None:
    """Records a pausing sandbox as paused, with where its snapshot is and when it paused."""
    db.transition(
        sandbox_id, [SandboxState.PAUSING], SandboxState.PAUSED,
        snapshot_dir=node_agent.snapshot_dir(sandbox_id), paused_at=db.now(),
    )


# ---------------------------------------------------------------------------
# Resuming
# ---------------------------------------------------------------------------

def resume(sandbox_id: str) -> SandboxRow:
    """Brings a paused sandbox back, waiting for a pause still under way; answers 409 unless it's paused."""
    require_state_in(require_sandbox(sandbox_id), WAIT_FOR_LOCK_STATES, NOT_PAUSED)
    with locks.lock_for(sandbox_id):
        resume_locked(sandbox_id)
    return require_sandbox(sandbox_id)


def ensure_running(sandbox_id: str) -> None:
    """Gets a sandbox ready for a command, resuming it first if it's paused; answers 404 or 409 if it can't."""
    row = require_sandbox(sandbox_id)
    if row.state == SandboxState.RUNNING:
        return   # no lock: the agent's own turn holds it while it calls exec
    require_state_in(row, WAIT_FOR_LOCK_STATES, NOT_RUNNING)
    with locks.lock_for(sandbox_id):   # waits for a pause or resume already under way
        resume_if_paused(sandbox_id)


def resume_if_paused(sandbox_id: str) -> None:
    """Brings a paused sandbox back if needed, or answers 409 if it can't take commands; caller holds the lock."""
    state = require_sandbox(sandbox_id).state
    if state == SandboxState.PAUSED:
        resume_locked(sandbox_id)
    elif state != SandboxState.RUNNING:
        raise conflict(NOT_RUNNING)


def resume_locked(sandbox_id: str) -> None:
    """Restarts a paused sandbox from its snapshot, answering 409 or 500 if it can't; caller holds the lock."""
    if not db.transition(sandbox_id, [SandboxState.PAUSED], SandboxState.RESUMING):
        raise conflict(NOT_PAUSED)
    try:
        node_agent.resume_sandbox(sandbox_id)
    except Exception as error:
        settle_resuming(sandbox_id)
        raise HTTPException(status_code=HTTP_SERVER_ERROR, detail=RESUME_FAILED) from error
    mark_resumed(sandbox_id)


def mark_resumed(sandbox_id: str) -> None:
    """Records a resuming sandbox as running again, clearing its snapshot details."""
    db.transition(sandbox_id, [SandboxState.RESUMING], SandboxState.RUNNING, snapshot_dir=None, paused_at=None)


# ---------------------------------------------------------------------------
# Settling a pause or resume that failed or was cut off by a crash
# ---------------------------------------------------------------------------

def vm_answers(sandbox_id: str) -> bool:
    """Checks the VM's process is alive and the program inside it replies."""
    return node_agent.is_sandbox_running(sandbox_id) and node_agent.guest_agent_is_healthy(sandbox_id)


def snapshot_is_complete(sandbox_id: str) -> bool:
    """Checks every file needed to resume is there, with the memory file at full size."""
    try:
        node_agent.require_resumable_files(sandbox_id)
        node_agent.check_snapshot_files(sandbox_id)
    except (FileNotFoundError, RuntimeError):
        return False
    return True


def try_unfreeze(sandbox_id: str) -> None:
    """Restarts a frozen VM's CPUs if its Firecracker is still there; does nothing otherwise."""
    try:
        with node_agent.firecracker_client(sandbox_id) as client:
            node_agent.unfreeze_vm(client)
    except Exception:
        pass   # not frozen, or Firecracker is gone: vm_answers() decides what happens next


def settle_pausing(sandbox_id: str) -> None:
    """Records where an unfinished pause really left a sandbox: running, paused, or gone."""
    if node_agent.is_sandbox_running(sandbox_id):
        try_unfreeze(sandbox_id)       # a pause can stop between freezing the VM and saving it
        if vm_answers(sandbox_id):
            db.transition(sandbox_id, [SandboxState.PAUSING], SandboxState.RUNNING)
            return
        node_agent.kill_firecracker(sandbox_id)   # alive but stuck: let the snapshot decide
    if snapshot_is_complete(sandbox_id):
        mark_paused(sandbox_id)
        return
    tear_down(sandbox_id)              # no VM and no usable snapshot: nothing left to bring back


def settle_resuming(sandbox_id: str) -> None:
    """Records where an unfinished resume really left a sandbox: running, or still paused."""
    if vm_answers(sandbox_id):
        mark_resumed(sandbox_id)
        return
    node_agent.kill_firecracker(sandbox_id)   # half-loaded VM; snap/ stays, so a later resume can retry
    db.transition(sandbox_id, [SandboxState.RESUMING], SandboxState.PAUSED)


def recover_interrupted_changes() -> None:
    """On startup, settles every sandbox a crash left in the middle of a pause or resume."""
    for row in db.list_live_sandboxes():
        if row.state == SandboxState.PAUSING:
            settle_pausing(row.id)
        elif row.state == SandboxState.RESUMING:
            settle_resuming(row.id)


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
