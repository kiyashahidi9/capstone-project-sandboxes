from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse

import node_agent                      # module import: keeps node_agent.create_sandbox etc. apart from endpoint names
from api import db
from api.models import (
    CreateSandboxResponse,
    ExecuteBackgroundResponse,
    ExecuteRequest,
    ExecuteResponse,
    SandboxInfo,
    SandboxListResponse,
    SandboxRow,
    SandboxState,
)


HTTP_CREATED = 201
HTTP_NOT_FOUND = 404
HTTP_CONFLICT = 409
HTTP_SERVER_ERROR = 500
HTTP_BAD_GATEWAY = 502
OK = {"ok": True}


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
# Lookups that stop a request early
# ---------------------------------------------------------------------------

def require_sandbox(sandbox_id: str) -> SandboxRow:
    """Finds a sandbox's record, or answers 404 if there is no such sandbox."""
    ...


def require_running(sandbox_id: str) -> SandboxRow:
    """Finds a sandbox's record, or answers 409 if it isn't running yet or anymore."""
    ...  # require_sandbox(...) first, so unknown IDs still get 404


# ---------------------------------------------------------------------------
# Creating a sandbox
# ---------------------------------------------------------------------------

def boot_sandbox(sandbox_id: str) -> node_agent.Sandbox:
    """Starts the VM for a recorded sandbox, or answers 500 after marking it destroyed."""
    ...  # node_agent.create_sandbox already cleaned up the host when it fails


def build_create_response(sandbox_id: str, boot_seconds: float) -> CreateSandboxResponse:
    """Describes a freshly booted sandbox to the client, including its boot time."""
    ...


# ---------------------------------------------------------------------------
# Destroying a sandbox
# ---------------------------------------------------------------------------

def is_destroyed(row: SandboxRow) -> bool:
    """Checks whether a sandbox has already been torn down."""
    ...


def tear_down(sandbox_id: str) -> None:
    """Removes a sandbox from the host, then records it as destroyed."""
    ...


# ---------------------------------------------------------------------------
# Talking to the guest agent
# ---------------------------------------------------------------------------

def guest_error_to_http(error: httpx.HTTPStatusError) -> HTTPException:
    """Turns a guest agent error into one for the client: its 4xx passes through, anything else is 502."""
    ...


def run_command(sandbox_id: str, request: ExecuteRequest) -> dict:
    """Runs a command inside the sandbox and gives back the guest agent's raw answer."""
    ...  # node_agent.exec(...); catch httpx.HTTPStatusError -> raise guest_error_to_http(...)


def to_execute_response(result: dict) -> ExecuteResponse:
    """Shapes a finished command's output for the client."""
    ...


def to_background_response(result: dict) -> ExecuteBackgroundResponse:
    """Shapes a background command's process number for the client."""
    ...


def fetch_file(sandbox_id: str, path: str) -> str:
    """Reads a file from inside the sandbox, or answers 404 if it isn't there."""
    ...  # FileNotFoundError -> HTTPException(HTTP_NOT_FOUND)


def store_file(sandbox_id: str, path: str, content: str) -> None:
    """Saves text to a file inside a running sandbox."""
    ...  # require_running(...) then node_agent.write_file(...)


# ---------------------------------------------------------------------------
# Endpoints: sandboxes
# ---------------------------------------------------------------------------

@app.post("/sandboxes", status_code=HTTP_CREATED, response_model=CreateSandboxResponse)
def create_sandbox() -> CreateSandboxResponse:
    """Makes a brand-new sandbox and tells the client how to reach it."""
    # sandbox_id = node_agent.new_sandbox_id()
    # db.insert_sandbox(sandbox_id)
    # sandbox = boot_sandbox(sandbox_id)
    # db.set_state(sandbox_id, SandboxState.RUNNING)
    # return build_create_response(sandbox_id, sandbox.boot_seconds)
    ...


@app.get("/sandboxes", response_model=SandboxListResponse)
def list_sandboxes() -> SandboxListResponse:
    """Shows every sandbox ever made, destroyed ones included, oldest first."""
    ...  # DECISION: "all" per spec -> db.list_sandboxes()


@app.get("/sandboxes/{sandbox_id}", response_model=SandboxInfo)
def get_sandbox(sandbox_id: str) -> SandboxInfo:
    """Shows one sandbox's details."""
    ...


@app.delete("/sandboxes/{sandbox_id}")
def delete_sandbox(sandbox_id: str) -> dict[str, bool]:
    """Shuts a sandbox down for good; asking twice is harmless."""
    # DECISION: idempotent -> already destroyed returns OK, no 409
    # row = require_sandbox(...); if not is_destroyed(row): tear_down(...); return OK
    ...


# ---------------------------------------------------------------------------
# Endpoints: inside a sandbox
# ---------------------------------------------------------------------------

@app.post(
    "/sandboxes/{sandbox_id}/exec",
    response_model=ExecuteResponse | ExecuteBackgroundResponse,
)
def exec_command(sandbox_id: str, request: ExecuteRequest) -> ExecuteResponse | ExecuteBackgroundResponse:
    """Runs a shell command in the sandbox for the client."""
    # require_running(...); result = run_command(...)
    # return to_background_response(result) if request.background else to_execute_response(result)
    ...


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
    # return OK
    ...
