from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from node_agent import guest_ip_for


# Limits for ExecuteRequest (seconds)
DEFAULT_EXECUTE_TIMEOUT = 600     # same default as the sandbox manager (guest_agent/)
MAX_EXECUTE_TIMEOUT = 3600        # caps how long one request can hold a server thread
DEFAULT_CWD = "/root"             # same default as the sandbox manager


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class SandboxState(StrEnum):
    """The states a sandbox can be in; must match the CHECK in db/schema.sql."""

    STARTING = "starting"
    RUNNING = "running"
    PAUSING = "pausing"
    PAUSED = "paused"
    RESUMING = "resuming"
    DESTROYED = "destroyed"


class TurnState(StrEnum):
    """Whether the agent is running a turn; must match the CHECK in db/schema.sql."""

    IDLE = "idle"
    WORKING = "working"


class EventType(StrEnum):
    """The kinds of event in a sandbox's conversation; listed in db/schema.sql."""

    USER_MESSAGE = "user_message"
    AGENT_OUTPUT = "agent_output"
    QUESTION = "question"
    TURN_STARTED = "turn_started"
    TURN_FINISHED = "turn_finished"
    TURN_FAILED = "turn_failed"
    PAUSED = "paused"
    RESUMED = "resumed"


# ---------------------------------------------------------------------------
# Database row (internal)
# ---------------------------------------------------------------------------

class SandboxRow(BaseModel):
    """One sandbox as stored in state.db, with its slot from the slots table."""

    id: str
    state: SandboxState
    created_at: int                 # unix seconds
    updated_at: int                 # unix seconds
    turn_state: TurnState = TurnState.IDLE
    snapshot_dir: str | None = None # host folder holding the snapshot; None unless paused
    paused_at: int | None = None    # unix seconds; None unless paused
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
    turn_state: TurnState           # working while the agent runs a turn
    paused_at: int | None = None    # unix seconds; None unless paused

    @classmethod
    def from_row(cls, row: SandboxRow) -> SandboxInfo:
        """Turns a database row into what clients are shown."""
        return cls(
            id=row.id,
            state=row.state,
            ip=ip_for_slot(row.n),
            created_at=row.created_at,
            updated_at=row.updated_at,
            turn_state=row.turn_state,
            paused_at=row.paused_at,
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


# ---------------------------------------------------------------------------
# Messages and events
# ---------------------------------------------------------------------------

class MessageRequest(BaseModel):
    """What the user sends to start a turn."""

    text: str = Field(
        min_length=1,
        description="The user's message, handed to the agent on stdin.",
    )


class MessageResponse(BaseModel):
    """Where the user's message landed in the event log."""

    seq: int


class Event(BaseModel):
    """One entry in a sandbox's conversation."""

    seq: int                        # 1, 2, 3, ... per sandbox
    type: EventType
    data: dict                      # depends on type, e.g. {"text": "..."} for user_message
    at: int                         # unix seconds


class EventListResponse(BaseModel):
    """The events a client hasn't seen yet, oldest first."""

    events: list[Event]
