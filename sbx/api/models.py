from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from node_agent import guest_ip_for


# Limits for ExecuteRequest (seconds)
DEFAULT_EXECUTE_TIMEOUT = 600     # same default as the guest agent
MAX_EXECUTE_TIMEOUT = 3600        # caps how long one request can hold a server thread
DEFAULT_CWD = "/root"             # same default as the guest agent


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class SandboxState(StrEnum):
    """The states a sandbox can be in; must match the CHECK in db/schema.sql."""

    STARTING = "starting"
    RUNNING = "running"
    DESTROYED = "destroyed"


# ---------------------------------------------------------------------------
# Database row (internal)
# ---------------------------------------------------------------------------

class SandboxRow(BaseModel):
    """One sandbox as stored in state.db, with its slot from the slots table."""

    id: str
    state: SandboxState
    created_at: int                 # unix seconds
    updated_at: int                 # unix seconds
    n: int | None = None            # network slot; None when it holds none


# ---------------------------------------------------------------------------
# Responses about sandboxes
# ---------------------------------------------------------------------------

class SandboxInfo(BaseModel):
    """One sandbox as clients see it: no slot number, but the IP they can reach."""

    id: str
    state: SandboxState
    ip: str | None                  # None when the sandbox holds no slot
    created_at: int                 # unix seconds
    updated_at: int                 # unix seconds

    @classmethod
    def from_row(cls, row: SandboxRow) -> SandboxInfo:
        """Turns a database row into what clients are shown."""
        return cls(
            id=row.id,
            state=row.state,
            ip=ip_for_slot(row.n),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


def ip_for_slot(n: int | None) -> str | None:
    """Gives the sandbox's IP address, or nothing if it has no network slot."""
    if n is None:
        return None
    return guest_ip_for(n)


class CreateSandboxResponse(SandboxInfo):
    """A newly created sandbox, plus how long it took to boot."""

    boot_seconds: float


class SandboxListResponse(BaseModel):
    """Every sandbox the API knows about."""

    sandboxes: list[SandboxInfo]


# ---------------------------------------------------------------------------
# Running commands
# ---------------------------------------------------------------------------

class ExecuteRequest(BaseModel):
    """What a client sends to run a command inside a sandbox."""

    cmd: str = Field(
        min_length=1,
        description="Shell command to run, e.g. 'npm install'. Each call starts a fresh shell.",
    )
    cwd: str = Field(
        default=DEFAULT_CWD,
        description="Folder the command starts in. Must exist, or the call fails with 400.",
    )
    background: bool = Field(
        default=False,
        description="If true, start the command and return its pid without waiting for it to finish.",
    )
    timeout: int = Field(
        default=DEFAULT_EXECUTE_TIMEOUT,
        gt=0,
        le=MAX_EXECUTE_TIMEOUT,
        description="Seconds before a foreground command is killed. Ignored when background is true.",
    )


class ExecuteResponse(BaseModel):
    """The result of a command the API waited for."""

    exit_code: int                  # 124 if it timed out
    stdout: str
    stderr: str
    timed_out: bool


class ExecuteBackgroundResponse(BaseModel):
    """The process number of a command left running; its output is in /tmp/bg-<pid>.log."""

    pid: int
