from __future__ import annotations

import logging
import os
import threading
from contextlib import contextmanager
from typing import Callable, Iterator

import httpx
from fastapi import HTTPException

import locks
import host_manager
import uploads
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
    UploadStatus,
)


logger = logging.getLogger(__name__)

DELETE_AFTER_ENV = "DELETE_PAUSED_AFTER_DAYS"   # in .env; blank (the default) never deletes anything
SECONDS_PER_DAY = 24 * 60 * 60
CLEANUP_INTERVAL_SECONDS = 60 * 60               # how often to look for forgotten sandboxes

HTTP_NOT_FOUND = 404
HTTP_CONFLICT = 409
HTTP_SERVER_ERROR = 500
HTTP_BAD_GATEWAY = 502
HTTP_SERVICE_UNAVAILABLE = 503
HTTP_CREATED = 201
HTTP_ACCEPTED = 202

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
    runner.stop_turn(sandbox_id)
    while not lock.acquire(timeout=LOCK_RETRY_SECONDS):
        runner.stop_turn(sandbox_id)
    try:
        yield
    finally:
        lock.release()


def require_turn(sandbox_id: str) -> None:
    """Marks the sandbox as working, or answers 409 if a turn is already running."""
    if not db.claim_turn(sandbox_id):
        raise HTTPException(status_code=HTTP_CONFLICT, detail="A turn is already running")


# ---------------------------------------------------------------------------
# Creating a sandbox
# ---------------------------------------------------------------------------

def boot_sandbox(sandbox_id: str) -> host_manager.Sandbox:
    """Starts the VM for a recorded sandbox, or marks it destroyed and answers 503 (host full) or 500."""
    try:
        return host_manager.create_sandbox(sandbox_id)
    except host_manager.NoFreeSlotsError as error:
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
        turn_state=info.turn_state,
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
    uploads.stop_upload(sandbox_id)   # its snapshot files are about to be deleted
    host_manager.destroy_sandbox(sandbox_id)
    db.set_state(sandbox_id, SandboxState.DESTROYED)
    run_logging_failure(uploads.delete_bucket_copies, sandbox_id)   # last: an unreachable bucket can't block a delete


def destroy(sandbox_id: str) -> None:
    """Shuts a sandbox down for good from any state, waiting for a pause or resume to finish first."""
    if is_destroyed(require_sandbox(sandbox_id)):
        return
    with lock_taken_from_agent(sandbox_id):
        if not is_destroyed(require_sandbox(sandbox_id)):
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
        uploads.start_upload(sandbox_id)   # still under the lock, so no resume can start the VM before the upload is known
    return require_sandbox(sandbox_id)


def save_snapshot(sandbox_id: str) -> None:
    """Writes the VM to snapshot files; if that fails, records where it really ended up and answers 500."""
    try:
        host_manager.pause_sandbox(sandbox_id)
    except Exception as error:
        settle_pausing(sandbox_id)
        raise HTTPException(status_code=HTTP_SERVER_ERROR, detail=PAUSE_FAILED) from error
    mark_paused(sandbox_id)


def mark_paused(sandbox_id: str) -> None:
    """Records a pausing sandbox as paused, with where its snapshot is and when it paused."""
    db.transition(
        sandbox_id, [SandboxState.PAUSING], SandboxState.PAUSED,
        snapshot_dir=host_manager.snapshot_dir(sandbox_id), paused_at=db.now(),
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
        return
    require_state_in(row, WAIT_FOR_LOCK_STATES, NOT_RUNNING)
    with locks.lock_for(sandbox_id):
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
    uploads.stop_upload(sandbox_id)   # the resumed VM writes to rootfs.ext4, so no upload may still be reading it
    try:
        restore_if_missing(sandbox_id)
        host_manager.resume_sandbox(sandbox_id)
    except Exception as error:
        settle_resuming(sandbox_id)
        raise HTTPException(status_code=HTTP_SERVER_ERROR, detail=RESUME_FAILED) from error
    mark_resumed(sandbox_id)


def restore_if_missing(sandbox_id: str) -> None:
    """Fetches the snapshot from the bucket when the local copy is gone or broken, so resume can still go on."""
    if not snapshot_is_complete(sandbox_id):
        uploads.restore_from_bucket(sandbox_id)


def mark_resumed(sandbox_id: str) -> None:
    """Records a resuming sandbox as running again, clearing its snapshot details."""
    db.transition(sandbox_id, [SandboxState.RESUMING], SandboxState.RUNNING, snapshot_dir=None, paused_at=None)


# ---------------------------------------------------------------------------
# Settling a pause or resume that failed or was cut off by a crash
# ---------------------------------------------------------------------------

def vm_answers(sandbox_id: str) -> bool:
    """Checks the VM's process is alive and the program inside it replies."""
    return host_manager.is_sandbox_running(sandbox_id) and host_manager.sandbox_manager_is_healthy(sandbox_id)


def snapshot_is_complete(sandbox_id: str) -> bool:
    """Checks every file needed to resume is there, with the memory file at full size."""
    try:
        host_manager.require_resumable_files(sandbox_id)
        host_manager.check_snapshot_files(sandbox_id)
    except (FileNotFoundError, RuntimeError):
        return False
    return True


def try_unfreeze(sandbox_id: str) -> None:
    """Restarts a frozen VM's CPUs if its Firecracker is still there; does nothing otherwise."""
    try:
        with host_manager.firecracker_client(sandbox_id) as client:
            host_manager.unfreeze_vm(client)
    except Exception:
        pass


def settle_pausing(sandbox_id: str) -> None:
    """Records where an unfinished pause really left a sandbox: running, paused, or gone."""
    if host_manager.is_sandbox_running(sandbox_id):
        try_unfreeze(sandbox_id)       # a pause can stop between freezing the VM and saving it
        if vm_answers(sandbox_id):
            db.transition(sandbox_id, [SandboxState.PAUSING], SandboxState.RUNNING)
            return
        host_manager.kill_firecracker(sandbox_id)   # alive but stuck: let the snapshot decide
    if snapshot_is_complete(sandbox_id):
        mark_paused(sandbox_id)
        return
    tear_down(sandbox_id)


def settle_resuming(sandbox_id: str) -> None:
    """Records where an unfinished resume really left a sandbox: running, or still paused."""
    if vm_answers(sandbox_id):
        mark_resumed(sandbox_id)
        return
    host_manager.kill_firecracker(sandbox_id)   # half-loaded VM; snap/ stays, so a later resume can retry
    db.transition(sandbox_id, [SandboxState.RESUMING], SandboxState.PAUSED)


def recover_interrupted_changes() -> None:
    """On startup, settles every sandbox a crash left in the middle of a pause or resume."""
    for row in db.list_live_sandboxes():
        if row.state == SandboxState.PAUSING:
            settle_pausing(row.id)
        elif row.state == SandboxState.RESUMING:
            settle_resuming(row.id)


# ---------------------------------------------------------------------------
# Startup cleanup: sandboxes a crash or a dead VM left behind (2a)
#
# Runs after recover_interrupted_changes(), so every live row is starting,
# running or paused, never half-way through a pause or resume.
# ---------------------------------------------------------------------------

def is_gone(row: SandboxRow) -> bool:
    """Checks whether a live sandbox can no longer be used, so it should be destroyed."""
    if row.state == SandboxState.STARTING:
        return True   # a crash cut off the create, so nobody was ever given this ID
    if row.state == SandboxState.PAUSED:
        # no VM on purpose: it's lost only if neither the host nor the bucket has a whole snapshot
        return not snapshot_is_complete(row.id) and not bucket_can_restore(row.id)
    return not host_manager.is_sandbox_running(row.id)


def bucket_can_restore(sandbox_id: str) -> bool:
    """Checks whether the bucket can still restore the sandbox; an unreachable bucket counts as yes."""
    try:
        return uploads.has_complete_copy(sandbox_id)
    except Exception:
        logger.exception("couldn't check the bucket for %s; keeping it", sandbox_id)
        return True


def run_logging_failure(step: Callable[[str], object], sandbox_id: str) -> None:
    """Runs one step on a sandbox, logging a failure instead of stopping the steps for the other sandboxes."""
    try:
        step(sandbox_id)
    except Exception:
        logger.exception("%s failed for %s; leaving it as it is", step.__name__, sandbox_id)


def destroy_gone_sandboxes() -> None:
    """Destroys every sandbox the database calls live but that can no longer be used."""
    for row in db.list_live_sandboxes():
        if is_gone(row):
            logger.warning("lost sandbox %s: it was %s, but its VM or snapshot is gone", row.id, row.state.value)
            run_logging_failure(tear_down, row.id)


def destroy_untracked_sandboxes() -> None:
    """Removes every sandbox left on the host that no live row keeps track of."""
    live_ids = {row.id for row in db.list_live_sandboxes()}
    for sandbox_id in host_manager.list_host_sandboxes():
        if sandbox_id not in live_ids:
            run_logging_failure(host_manager.destroy_sandbox, sandbox_id)


def clean_up_after_restart() -> None:
    """On startup, destroys sandboxes whose VM or snapshot is gone, then leftovers nothing tracks."""
    destroy_gone_sandboxes()
    destroy_untracked_sandboxes()


def needs_upload(row: SandboxRow) -> bool:
    """Checks whether a sandbox is paused but its bucket copy isn't complete (cut off, failed, or never made)."""
    return row.state == SandboxState.PAUSED and row.upload_status != UploadStatus.DONE


def restart_unfinished_uploads() -> None:
    """On startup, copies again every paused sandbox whose bucket copy a shutdown or failure left incomplete."""
    for row in db.list_live_sandboxes():
        if needs_upload(row):
            with locks.lock_for(row.id):   # start_upload expects the lock held, as in pause()
                uploads.start_upload(row.id)


# ---------------------------------------------------------------------------
# Deleting forgotten sandboxes (6b): off unless DELETE_PAUSED_AFTER_DAYS is set
# ---------------------------------------------------------------------------

def delete_after_days() -> int | None:
    """Gives how many days a sandbox may stay paused before it's deleted, or None when that's turned off."""
    setting = os.environ.get(DELETE_AFTER_ENV, "").strip()
    if not setting:
        return None
    if not setting.isdigit() or int(setting) < 1:
        logger.warning("%s=%r isn't a whole number of days (1 or more); deleting nothing", DELETE_AFTER_ENV, setting)
        return None
    return int(setting)


def is_forgotten(row: SandboxRow, cutoff: int) -> bool:
    """Checks whether a sandbox has stayed paused since before the cutoff time."""
    return row.state == SandboxState.PAUSED and row.paused_at is not None and row.paused_at < cutoff


def delete_forgotten_sandboxes() -> None:
    """Deletes every sandbox paused for longer than the setting allows, with its bucket copies; off by default."""
    days = delete_after_days()
    if days is None:
        return
    cutoff = db.now() - days * SECONDS_PER_DAY
    for row in db.list_live_sandboxes():
        if is_forgotten(row, cutoff):
            logger.warning("deleting %s: paused for more than %d days", row.id, days)
            run_logging_failure(destroy, row.id)


def run_cleanup_until_stopped(stop: threading.Event) -> None:
    """Looks for forgotten sandboxes now and then once an hour, until the server shuts down."""
    while not stop.is_set():
        delete_forgotten_sandboxes()
        stop.wait(CLEANUP_INTERVAL_SECONDS)


# ---------------------------------------------------------------------------
# Shutdown (6c): pause every running sandbox, so it survives the VM stopping.
# Runs inside the server because the locks and running turns only exist here.
# ---------------------------------------------------------------------------

def pause_running_sandboxes() -> None:
    """Saves every running sandbox to a snapshot, logging any that fail and carrying on with the rest."""
    for row in db.list_live_sandboxes():
        if row.state == SandboxState.RUNNING:
            run_logging_failure(pause, row.id)


# ---------------------------------------------------------------------------
# Talking to the sandbox manager (sandbox_manager/)
# ---------------------------------------------------------------------------

def guest_error_to_http(error: httpx.HTTPStatusError) -> HTTPException:
    """Turns a sandbox manager error into one for the client: its 4xx passes through, anything else is 502."""
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
    """Runs a command inside the sandbox and gives back the sandbox manager's raw answer."""
    try:
        return host_manager.exec(
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
        return host_manager.read_file(sandbox_id, path)
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
        host_manager.write_file(sandbox_id, path, content)
    except httpx.HTTPStatusError as error:
        raise guest_error_to_http(error) from error
    except httpx.RequestError as error:
        raise HTTPException(status_code=HTTP_BAD_GATEWAY, detail="Guest agent failed") from error


# ---------------------------------------------------------------------------
# Turns
# ---------------------------------------------------------------------------

def start_turn_in_background(sandbox_id: str) -> None:
    """
    Runs the agent for this sandbox's new message without making the request wait
    daemon thread, so a turn in progress doesn't block the server from shutting down
    """
    thread = threading.Thread(
        target=runner.run_turn,
        args=(sandbox_id,),
        name=f"turn-{sandbox_id}",
        daemon=True,
    )
    thread.start()
