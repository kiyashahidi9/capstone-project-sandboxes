from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from host_manager import guest_ip_for


# Limits for ExecuteRequest (seconds)
DEFAULT_EXECUTE_TIMEOUT = 600     # same default as the sandbox manager (sandbox_manager/)
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


class UploadStatus(StrEnum):
    """How far the copy of a sandbox's latest snapshot to the bucket got; must match the CHECK in db/schema.sql."""

    PENDING = "pending"
    UPLOADING = "uploading"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"       # the sandbox resumed before the upload finished


class EventType(StrEnum):
    """The kinds of event in a sandbox's conversation; listed in db/schema.sql."""

    USER_MESSAGE = "user_message"
    AGENT_OUTPUT = "agent_output"
    QUESTION = "question"
    LLM_REQUEST = "llm_request"
    LLM_RESPONSE = "llm_response"
    TOOL_STARTED = "tool_started"
    TOOL_COMPLETED = "tool_completed"
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
    upload_status: UploadStatus | None = None   # copy of the latest snapshot to the bucket; None if never uploaded
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


# ---------------------------------------------------------------------------
# Observability: one summary per turn (docs/observability.md)
# ---------------------------------------------------------------------------

class TurnStatus(StrEnum):
    """How a turn went, worked out from its events."""

    RUNNING = "running"             # the agent is still working and recently recorded something
    UNRESPONSIVE = "unresponsive"   # still working, but nothing recorded for a while
    FINISHED = "finished"           # turn_finished
    FAILED = "failed"               # turn_failed for any reason but a pause
    PAUSED = "paused"               # a pause cut the turn off; not a failure
    INCOMPLETE = "incomplete"       # no end recorded and the agent isn't working; shouldn't happen


class FailureCause(StrEnum):
    """Whose fault a failed turn was."""

    AGENT = "agent"                 # the VM was healthy, so the agent itself crashed or hung
    SANDBOX = "sandbox"             # the VM or the program inside it stopped answering
    HOST = "host"                   # our side: server restart, missing AGENT_CMD, runner error
    PAUSED = "paused"               # a user or auto-pause stopped it
    UNKNOWN = "unknown"             # the VM check itself failed


class TurnSummary(BaseModel):
    """One turn (one user message and everything done for it), for investigating failures."""

    turn: int                       # 1, 2, 3, ... per sandbox, in message order
    message: str                    # what the user asked: the turn's user_message text
    status: TurnStatus
    reason: str | None = None       # from turn_failed: exit, timeout, paused, server restarted, ...
    cause: FailureCause | None = None
    exit_code: int | None = None
    stderr_tail: str | None = None  # the agent's last 50 lines of stderr, when it failed
    started_at: int                 # unix seconds, when the user's message arrived
    ended_at: int | None = None     # unix seconds; None while running
    first_seq: int                  # the turn's events are first_seq..last_seq
    last_seq: int
    seconds_since_last_event: int
    last_action: Event | None = None      # the last step or output before the turn ended
    unfinished_step: Event | None = None  # a tool_started or llm_request that never got its answer


class TurnListResponse(BaseModel):
    """Every turn of one sandbox, oldest first."""

    turns: list[TurnSummary]


# ---------------------------------------------------------------------------
# Replay: what the model was sent and what came back, one model call at a time
# ---------------------------------------------------------------------------

class ReplayTool(BaseModel):
    """One tool call the model asked for, and what it got back."""

    tool_call_id: str
    name: str | None = None
    arguments: Any = None
    result: Any = None              # exactly what the model was given; None if it never finished
    finished: bool


class ReplayStep(BaseModel):
    """One model call in a turn, rebuilt from events with sdk/history.py."""

    step: int                       # 1, 2, 3, ... within the turn
    seq: int                        # the llm_request event's seq
    at: int                         # unix seconds
    model: str | None = None
    messages: list[dict]            # what the model was sent, system prompt first
    new_messages: int               # how many of them are new since the previous step (or this turn, for step 1)
    matches_record: bool | None     # the rebuild equals the messages the agent recorded; None if it recorded none
    response: dict | None = None    # the llm_response's data; None if the model never answered
    tools: list[ReplayTool]


class ReplayResponse(BaseModel):
    """Every model call of one turn, in order."""

    turn: int
    steps: list[ReplayStep]
