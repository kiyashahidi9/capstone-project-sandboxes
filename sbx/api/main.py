from __future__ import annotations

import json
import queue
import threading
from collections.abc import Generator, Iterator
from contextlib import asynccontextmanager
from typing import AsyncIterator

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import PlainTextResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

import host_manager
from api import db, llm_proxy
from api.auth import require_admin, require_admin_key_set, require_admin_or_turn
from api.helpers import (
    HTTP_ACCEPTED,
    HTTP_CREATED,
    boot_sandbox,
    build_create_response,
    clean_up_after_restart,
    pause_running_sandboxes,
    restart_unfinished_uploads,
    run_cleanup_until_stopped,
    destroy,
    ensure_running,
    fetch_file,
    pause,
    recover_interrupted_changes,
    require_sandbox,
    require_turn,
    resume,
    run_command,
    start_turn_in_background,
    store_file,
    text_from_body,
    to_background_response,
    to_execute_response,
)
from api.models import (
    CreateSandboxResponse,
    Event,
    EventListResponse,
    EventType,
    ExecuteBackgroundResponse,
    ExecuteRequest,
    ExecuteResponse,
    MessageRequest,
    MessageResponse,
    SandboxInfo,
    SandboxListResponse,
    SandboxState,
)
from control.live import SampleMemory
from control.heartbeat import run_until_stopped
from llm_providers import PROXY_PREFIX
from runner import runner


# AGENT_CMD, AGENT_TURN_TIMEOUT, SANDBOX_ADMIN_KEY and the AI key come from .env at the repo root
load_dotenv()

# who may call each route (api/auth.py)
ADMIN_ONLY = Depends(require_admin)
ADMIN_OR_TURN = Depends(require_admin_or_turn)   # exec and files: also the turn's own agent


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Gets ready and settles what a crash or reboot left behind; on shutdown, pauses every running sandbox."""
    require_admin_key_set()
    db.create_tables()
    runner.recover_interrupted_turns()
    recover_interrupted_changes()
    clean_up_after_restart()
    restart_unfinished_uploads()
    stop = threading.Event()
    memory = SampleMemory()
    beat = threading.Thread(
        target=run_until_stopped,
        args=(memory, stop),
        name="sbx-beat",
        daemon=True,
    )
    app.state.beat_stop = stop
    app.state.beat_memory = memory
    app.state.beat_thread = beat
    beat.start()
    cleanup = start_cleanup_thread(stop)
    try:
        yield
    finally:
        stop.set()
        beat.join(timeout=2)   # first, so the heartbeat can't resume anything while we pause
        cleanup.join(timeout=2)
        pause_running_sandboxes()


def start_cleanup_thread(stop: threading.Event) -> threading.Thread:
    """Starts the hourly check that deletes forgotten sandboxes (6b), ending when the server shuts down."""
    cleanup = threading.Thread(target=run_cleanup_until_stopped, args=(stop,), name="sbx-cleanup", daemon=True)
    cleanup.start()
    return cleanup


app = FastAPI(title="sbx", lifespan=lifespan)

# How long a stream waits for the next event before writing a blank line.
# The blank line keeps the connection open and lets a closed client be noticed.
# Clients skip blank lines.
STREAM_POLL_SECONDS = 1.0


# ---------------------------------------------------------------------------
# Endpoints: sandboxes
# ---------------------------------------------------------------------------

@app.post(
    "/sandboxes",
    status_code=HTTP_CREATED,
    response_model=CreateSandboxResponse,
    dependencies=[ADMIN_ONLY],
)
def create_sandbox() -> CreateSandboxResponse:
    """Makes a brand-new sandbox and tells the client how to reach it."""
    sandbox_id = host_manager.new_sandbox_id()
    db.insert_sandbox(sandbox_id)
    sandbox = boot_sandbox(sandbox_id)
    db.set_state(sandbox_id, SandboxState.RUNNING)
    return build_create_response(sandbox_id, sandbox.boot_seconds)


@app.get("/sandboxes", response_model=SandboxListResponse, dependencies=[ADMIN_ONLY])
def list_sandboxes() -> SandboxListResponse:
    """Shows every sandbox ever made, destroyed ones included, oldest first."""
    rows = db.list_sandboxes()
    return SandboxListResponse(sandboxes=[SandboxInfo.from_row(row) for row in rows])


@app.get("/sandboxes/{sandbox_id}", response_model=SandboxInfo, dependencies=[ADMIN_ONLY])
def get_sandbox(sandbox_id: str) -> SandboxInfo:
    """Shows one sandbox's details."""
    return SandboxInfo.from_row(require_sandbox(sandbox_id))


@app.delete("/sandboxes/{sandbox_id}", dependencies=[ADMIN_ONLY])
def delete_sandbox(sandbox_id: str) -> dict[str, bool]:
    """Shuts a sandbox down for good, after any pause under way; asking twice is harmless."""
    destroy(sandbox_id)
    return {"ok": True}


@app.post("/sandboxes/{sandbox_id}/pause", response_model=SandboxInfo, dependencies=[ADMIN_ONLY])
def pause_sandbox(sandbox_id: str) -> SandboxInfo:
    """Saves a running sandbox to disk and frees its memory, cutting off the agent's turn."""
    return SandboxInfo.from_row(pause(sandbox_id))


@app.post("/sandboxes/{sandbox_id}/resume", response_model=SandboxInfo, dependencies=[ADMIN_ONLY])
def resume_sandbox(sandbox_id: str) -> SandboxInfo:
    """Brings a paused sandbox back exactly where it left off."""
    return SandboxInfo.from_row(resume(sandbox_id))


# ---------------------------------------------------------------------------
# Endpoints: inside a sandbox
# ---------------------------------------------------------------------------

@app.post(
    "/sandboxes/{sandbox_id}/exec",
    response_model=ExecuteResponse | ExecuteBackgroundResponse,
    dependencies=[ADMIN_OR_TURN],
)
def exec_command(sandbox_id: str, request: ExecuteRequest) -> ExecuteResponse | ExecuteBackgroundResponse:
    """Runs a shell command in the sandbox for the client, resuming it first if it's paused."""
    ensure_running(sandbox_id)
    result = run_command(sandbox_id, request)
    if request.background:
        return to_background_response(result)
    return to_execute_response(result)


@app.get("/sandboxes/{sandbox_id}/files", response_class=PlainTextResponse, dependencies=[ADMIN_OR_TURN])
def read_file(sandbox_id: str, path: str) -> str:
    """Sends the client a file's contents from inside the sandbox, resuming it first if it's paused."""
    ensure_running(sandbox_id)
    return fetch_file(sandbox_id, path)


@app.put("/sandboxes/{sandbox_id}/files", dependencies=[ADMIN_OR_TURN])
async def write_file(sandbox_id: str, path: str, request: Request) -> dict[str, bool]:
    """Saves the uploaded text to a file inside the sandbox, resuming it first if it's paused."""
    await run_in_threadpool(ensure_running, sandbox_id)
    content = text_from_body(await request.body())
    await run_in_threadpool(store_file, sandbox_id, path, content)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Endpoint: the key proxy (api/llm_proxy.py)
# ---------------------------------------------------------------------------

@app.post(PROXY_PREFIX + "/{provider_name}/{path:path}")
async def proxy_model_call(provider_name: str, path: str, request: Request) -> StreamingResponse:
    """Passes an agent's model call on to its provider with the client's key, which the agent never sees."""
    provider = llm_proxy.require_provider(provider_name)
    llm_proxy.require_allowed_path(provider, path)
    llm_proxy.require_turn(request)
    key = llm_proxy.require_key(provider)
    return await llm_proxy.forward(request, provider, path, key)


# ---------------------------------------------------------------------------
# Endpoints: the conversation
# ---------------------------------------------------------------------------

@app.post(
    "/sandboxes/{sandbox_id}/messages",
    status_code=HTTP_ACCEPTED,
    response_model=MessageResponse,
    dependencies=[ADMIN_ONLY],
)
def send_message(sandbox_id: str, request: MessageRequest) -> MessageResponse:
    """
    Saves the user's message and starts the agent on it in the background
    resumes a paused sandbox first, so the reply is just slower (plan 4c)
    answers 409 if the sandbox can't run (starting, destroyed) or a turn is already running
    """
    ensure_running(sandbox_id)
    require_turn(sandbox_id)

    seq = db.append_event(sandbox_id, EventType.USER_MESSAGE, {"text": request.text})

    start_turn_in_background(sandbox_id)
    return MessageResponse(seq=seq)


@app.get("/sandboxes/{sandbox_id}/events", response_model=EventListResponse, dependencies=[ADMIN_ONLY])
def list_events(sandbox_id: str, after: int = Query(default=0, ge=0)) -> EventListResponse:
    """Shows every event after seq `after`, oldest first; works for destroyed sandboxes too."""
    require_sandbox(sandbox_id)
    return EventListResponse(events=db.list_events(sandbox_id, after))


@app.get("/sandboxes/{sandbox_id}/events/stream", dependencies=[ADMIN_ONLY])
def stream_events(sandbox_id: str, after: int = Query(default=0, ge=0)) -> StreamingResponse:
    """Sends the events after `after`, then each new one, until the sandbox is destroyed."""
    require_sandbox(sandbox_id)
    return StreamingResponse(iter_event_lines(sandbox_id, after), media_type="application/x-ndjson")


def iter_event_lines(sandbox_id: str, after: int) -> Iterator[str]:
    """Yields one JSON event per line. A destroyed sandbox ends the stream."""
    updates = db.listen(sandbox_id)
    last: int = after
    try:
        while True:
            last = yield from event_lines_after(sandbox_id, last)
            row = db.get_sandbox(sandbox_id)
            if row is None or row.state is SandboxState.DESTROYED:
                return
            try:
                seq: int | None = updates.get(timeout=STREAM_POLL_SECONDS)
            except queue.Empty:
                yield "\n"
                continue
            if seq is None:
                yield from event_lines_after(sandbox_id, last)
                return
    finally:
        db.unlisten(sandbox_id, updates)


def event_lines_after(sandbox_id: str, after: int) -> Generator[str, None, int]:
    """Yields stored events after `after`, and returns the last seq yielded, or `after`."""
    last: int = after
    for event in db.list_events(sandbox_id, after):
        last = event.seq
        yield event_line(event)
    return last


def event_line(event: Event) -> str:
    """One event as a single JSON line."""
    return json.dumps(event.model_dump(mode="json")) + "\n"
