from __future__ import annotations

import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from api.models import SandboxRow, SandboxState
from node_agent import db_path


SCHEMA_PATH = Path(__file__).resolve().parent.parent / "db" / "schema.sql"
DB_BUSY_TIMEOUT = 5.0             # seconds a writer waits for another writer before giving up

# Sandboxes joined to slots; LEFT JOIN keeps sandboxes that hold no slot (their n is NULL).
SELECT_SANDBOXES = """
    SELECT sandboxes.id, sandboxes.state, sandboxes.created_at, sandboxes.updated_at, slots.n
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
        timeout=DB_BUSY_TIMEOUT,  # a second writer waits instead of failing with "database is locked"
        isolation_level=None,     # autocommit: each statement is its own transaction
    )
    conn.execute("PRAGMA journal_mode=WAL")   # readers aren't blocked while someone writes
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
