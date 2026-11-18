from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from api.models import Event, EventType, SandboxRow, SandboxState, TurnState
from host_manager import db_path


SCHEMA_PATH = Path(__file__).resolve().parent.parent / "db" / "schema.sql"
DB_BUSY_TIMEOUT = 5.0             # seconds a writer waits for another writer before giving up

# Sandboxes joined to slots; LEFT JOIN keeps sandboxes that hold no slot (their n is NULL).
SELECT_SANDBOXES = """
    SELECT sandboxes.id, sandboxes.state, sandboxes.created_at, sandboxes.updated_at,
           sandboxes.turn_state, sandboxes.snapshot_dir, sandboxes.paused_at, slots.n
    FROM sandboxes
    LEFT JOIN slots ON slots.sandbox_id = sandboxes.id
"""
OLDEST_FIRST = "ORDER BY sandboxes.created_at, sandboxes.id"


# ---------------------------------------------------------------------------
# Connections and setup
# ---------------------------------------------------------------------------

def connect() -> sqlite3.Connection:
    """Opens a fresh connection to the state database using the shared SQLite rules."""
    conn = sqlite3.connect(
        db_path(),
        timeout=DB_BUSY_TIMEOUT,
        isolation_level=None,
    )
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def make_db_folder() -> None:
    """Creates the folder that holds the database file, if it isn't there yet."""
    os.makedirs(os.path.dirname(db_path()), exist_ok=True)


def create_tables() -> None:
    """Sets up the database tables from db/schema.sql; safe to run every startup."""
    make_db_folder()
    with closing(connect()) as conn:
        conn.executescript(SCHEMA_PATH.read_text())
        rebuild_sandboxes_table(conn)


def sandboxes_table_from_schema() -> str:
    """Gives the CREATE TABLE statement for sandboxes from db/schema.sql, so the table is defined in one place."""
    with closing(sqlite3.connect(":memory:")) as scratch:
        scratch.executescript(SCHEMA_PATH.read_text())
        (sql,) = scratch.execute("SELECT sql FROM sqlite_master WHERE name = 'sandboxes'").fetchone()
    return sql


def comparable_sql(sql: str) -> str:
    """Drops comments and extra spacing, so only real changes to a table count as different."""
    without_comments = re.sub(r"--[^\n]*", "", sql)
    return " ".join(without_comments.split())


def live_sandboxes_table(conn: sqlite3.Connection) -> str:
    """Gives the CREATE TABLE statement the database's sandboxes table was made with."""
    (sql,) = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'sandboxes'").fetchone()
    return sql


def column_names(conn: sqlite3.Connection, table: str) -> list[str]:
    """Lists a table's columns in order."""
    return [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]


def shared_columns(conn: sqlite3.Connection, old_table: str, new_table: str) -> list[str]:
    """Lists the columns two tables have in common, in the old table's order."""
    new_columns = set(column_names(conn, new_table))
    return [name for name in column_names(conn, old_table) if name in new_columns]


def rebuild_sandboxes_table(conn: sqlite3.Connection) -> None:
    """Remakes the sandboxes table, keeping every row, if schema.sql now defines it differently."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        wanted = sandboxes_table_from_schema()
        if comparable_sql(live_sandboxes_table(conn)) != comparable_sql(wanted):
            replace_sandboxes_table(conn, wanted)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def replace_sandboxes_table(conn: sqlite3.Connection, create_sql: str) -> None:
    """Swaps the sandboxes table for a new one, copying over every column the two share."""
    conn.execute("ALTER TABLE sandboxes RENAME TO sandboxes_old")
    conn.execute(create_sql)
    shared = ", ".join(shared_columns(conn, "sandboxes_old", "sandboxes"))
    conn.execute(f"INSERT INTO sandboxes ({shared}) SELECT {shared} FROM sandboxes_old")
    conn.execute("DROP TABLE sandboxes_old")


def now() -> int:
    """Gives the current time in whole seconds."""
    return int(time.time())


def to_sandbox_row(row: sqlite3.Row) -> SandboxRow:
    """Turns one database result into a SandboxRow."""
    return SandboxRow(**dict(row))


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def insert_sandbox(sandbox_id: str) -> None:
    """Records a new sandbox that is just starting up."""
    created_at = now()
    with closing(connect()) as conn:
        conn.execute(
            "INSERT INTO sandboxes (id, state, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (sandbox_id, SandboxState.STARTING, created_at, created_at),
        )


def set_state(sandbox_id: str, state: SandboxState) -> bool:
    """Changes a sandbox's state and stamps the time; says whether the sandbox was found."""
    with closing(connect()) as conn:
        cursor = conn.execute(
            "UPDATE sandboxes SET state = ?, updated_at = ? WHERE id = ?",
            (state, now(), sandbox_id),
        )
    return cursor.rowcount == 1


def claim_turn(sandbox_id: str) -> bool:
    """
    Marks an idle sandbox as working
    one statement, so two requests at once can't both win
    says whether this call won
    """
    with closing(connect()) as conn:
        cursor = conn.execute(
            "UPDATE sandboxes SET turn_state = ? WHERE id = ? AND turn_state = ?",
            (TurnState.WORKING, sandbox_id, TurnState.IDLE),
        )
    return cursor.rowcount == 1


# Columns transition() may set alongside the state; names go into the SQL, so only these are allowed.
TRANSITION_FIELDS = frozenset({"snapshot_dir", "paused_at"})


def require_transition_fields(fields: dict) -> None:
    """Refuses any column transition() isn't allowed to set, since column names go into the SQL."""
    unknown = set(fields) - TRANSITION_FIELDS
    if unknown:
        raise ValueError(f"transition can't set {sorted(unknown)}")


def transition(
    sandbox_id: str,
    from_states: list[SandboxState],
    to_state: SandboxState,
    **fields: str | int | None,
) -> bool:
    """Moves a sandbox to a new state only if it's in one of the expected ones; says whether it moved."""
    require_transition_fields(fields)
    assignments = ", ".join(["state = ?", "updated_at = ?", *(f"{name} = ?" for name in fields)])
    placeholders = ", ".join("?" for _ in from_states)
    with closing(connect()) as conn:
        cursor = conn.execute(
            f"UPDATE sandboxes SET {assignments} WHERE id = ? AND state IN ({placeholders})",
            (to_state, now(), *fields.values(), sandbox_id, *from_states),
        )
    return cursor.rowcount == 1


def set_turn_state(sandbox_id: str, turn_state: TurnState) -> None:
    """Sets whether the sandbox's agent is running a turn."""
    with closing(connect()) as conn:
        conn.execute(
            "UPDATE sandboxes SET turn_state = ? WHERE id = ?",
            (turn_state, sandbox_id),
        )


def append_event(sandbox_id: str, event_type: EventType, data: dict) -> int:
    """
    Adds an event to the end of the sandbox's conversation
    one statement, so two writers can't get the same seq
    returns the new event's seq
    """
    with closing(connect()) as conn:
        row = conn.execute(
            """
            INSERT INTO events (sandbox_id, seq, type, data, at)
            SELECT ?, COALESCE(MAX(seq), 0) + 1, ?, ?, ?
            FROM events WHERE sandbox_id = ?
            RETURNING seq
            """,
            (
                sandbox_id,
                event_type,
                json.dumps(data),
                now(),
                sandbox_id,
            ),
        ).fetchone()
    return row["seq"]


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def get_sandbox(sandbox_id: str) -> SandboxRow | None:
    """Looks up one sandbox, or gives nothing if there is no such sandbox."""
    with closing(connect()) as conn:
        row = conn.execute(f"{SELECT_SANDBOXES} WHERE sandboxes.id = ?", (sandbox_id,)).fetchone()
    if row is None:
        return None
    return to_sandbox_row(row)


def list_sandboxes() -> list[SandboxRow]:
    """Lists every sandbox ever created, oldest first."""
    with closing(connect()) as conn:
        rows = conn.execute(f"{SELECT_SANDBOXES} {OLDEST_FIRST}").fetchall()
    return [to_sandbox_row(row) for row in rows]


def list_live_sandboxes() -> list[SandboxRow]:
    """Lists the sandboxes that haven't been destroyed, oldest first."""
    with closing(connect()) as conn:
        rows = conn.execute(
            f"{SELECT_SANDBOXES} WHERE sandboxes.state != ? {OLDEST_FIRST}",
            (SandboxState.DESTROYED,),
        ).fetchall()
    return [to_sandbox_row(row) for row in rows]


def list_events(sandbox_id: str, after: int = 0) -> list[Event]:
    """Lists the sandbox's events with seq greater than after, oldest first."""
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT seq, type, data, at FROM events WHERE sandbox_id = ? AND seq > ? ORDER BY seq",
            (sandbox_id, after),
        ).fetchall()
    return [
        Event(seq=row["seq"], type=row["type"], data=json.loads(row["data"]), at=row["at"])
        for row in rows
    ]
