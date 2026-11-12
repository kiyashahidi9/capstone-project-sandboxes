"""HTTP routes for sandboxes. Helper functions live in helpers.py."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

import node_agent
from api import db
from api.helpers import (
    HTTP_CREATED,
    boot_sandbox,
    build_create_response,
    is_destroyed,
    require_running,
    require_sandbox,
    run_command,
    tear_down,
    to_background_response,
    to_execute_response,
)
from api.models import (
    CreateSandboxResponse,
    ExecuteBackgroundResponse,
    ExecuteRequest,
    ExecuteResponse,
    SandboxInfo,
    SandboxListResponse,
    SandboxState,
)


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Gets the database ready before the server starts taking requests."""
    ...  # db.create_tables(); later: reconcile the DB with node_agent.list_host_sandboxes()
    yield


app = FastAPI(title="sbx", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Endpoints: sandboxes
# ---------------------------------------------------------------------------

@app.post("/sandboxes", status_code=HTTP_CREATED, response_model=CreateSandboxResponse)
def create_sandbox() -> CreateSandboxResponse:
    """Makes a brand-new sandbox and tells the client how to reach it."""
    sandbox_id = node_agent.new_sandbox_id()
    db.insert_sandbox(sandbox_id)
    sandbox = boot_sandbox(sandbox_id)
    db.set_state(sandbox_id, SandboxState.RUNNING)
    return build_create_response(sandbox_id, sandbox.boot_seconds)


@app.get("/sandboxes", response_model=SandboxListResponse)
def list_sandboxes() -> SandboxListResponse:
    """Shows every sandbox ever made, destroyed ones included, oldest first."""
    rows = db.list_sandboxes()
    return SandboxListResponse(sandboxes=[SandboxInfo.from_row(row) for row in rows])


@app.get("/sandboxes/{sandbox_id}", response_model=SandboxInfo)
def get_sandbox(sandbox_id: str) -> SandboxInfo:
    """Shows one sandbox's details."""
    return SandboxInfo.from_row(require_sandbox(sandbox_id))


@app.delete("/sandboxes/{sandbox_id}")
def delete_sandbox(sandbox_id: str) -> dict[str, bool]:
    """Shuts a sandbox down for good; asking twice is harmless."""
    row = require_sandbox(sandbox_id)
    if not is_destroyed(row):
        tear_down(sandbox_id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Endpoints: inside a sandbox
# ---------------------------------------------------------------------------

@app.post(
    "/sandboxes/{sandbox_id}/exec",
    response_model=ExecuteResponse | ExecuteBackgroundResponse,
)
def exec_command(sandbox_id: str, request: ExecuteRequest) -> ExecuteResponse | ExecuteBackgroundResponse:
    """Runs a shell command in the sandbox for the client."""
    require_running(sandbox_id)
    result = run_command(sandbox_id, request)
    if request.background:
        return to_background_response(result)
    return to_execute_response(result)


@app.get("/sandboxes/{sandbox_id}/files", response_class=PlainTextResponse)
def read_file(sandbox_id: str, path: str) -> str:
    """Sends the client a file's contents from inside the sandbox as plain text."""
    ...  # DECISION: raw text like the guest agent; require_running(...) then fetch_file(...)


@app.put("/sandboxes/{sandbox_id}/files")
async def write_file(sandbox_id: str, path: str, request: Request) -> dict[str, bool]:
    """Saves the uploaded text to a file inside the sandbox."""
    # async only to await request.body(); blocking work goes through the thread pool:
    # content = (await request.body()).decode()
    # await run_in_threadpool(store_file, sandbox_id, path, content)
    # return {"ok": True}
    ...
