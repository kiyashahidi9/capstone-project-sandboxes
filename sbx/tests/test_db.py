import re
import sqlite3
from contextlib import closing

import pytest

import node_agent
from api import db
from api.models import SandboxState


ID_A = "sbx_" + "a" * 32
ID_B = "sbx_" + "b" * 32
ID_C = "sbx_" + "c" * 32


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """Points the state database at a fresh temporary file with the tables created."""
    monkeypatch.setenv("SBX_DB", str(tmp_path / "state.db"))
    db.create_tables()


@pytest.fixture
def clock(monkeypatch):
    """Replaces the clock with one the test sets by hand."""
    fake = {"now": 1000}
    monkeypatch.setattr(db, "now", lambda: fake["now"])
    return fake


def claim_slot(n, sandbox_id):
    """Gives a sandbox a network slot the way node_agent.py does."""
    with closing(db.connect()) as conn:
        conn.execute("INSERT INTO slots (n, sandbox_id) VALUES (?, ?)", (n, sandbox_id))


def slots_table_sql(schema_sql):
    """Builds a throwaway database from a schema and returns its slots table, comments and spacing removed."""
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.executescript(schema_sql)
        (sql,) = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'slots'").fetchone()
    without_comments = re.sub(r"--[^\n]*", "", sql)
    return " ".join(without_comments.split())


# --- setup ------------------------------------------------------------------

def test_schema_file_matches_node_agent_slots_schema():
    assert slots_table_sql(db.SCHEMA_PATH.read_text()) == slots_table_sql(node_agent.SLOTS_SCHEMA)


def test_create_tables_is_safe_to_run_twice():
    db.create_tables()


def test_connections_use_wal_mode():
    with closing(db.connect()) as conn:
        (mode,) = conn.execute("PRAGMA journal_mode").fetchone()
    assert mode == "wal"


# --- writes -----------------------------------------------------------------

def test_insert_starts_in_starting_with_matching_times(clock):
    db.insert_sandbox(ID_A)
    row = db.get_sandbox(ID_A)
    assert row.state == SandboxState.STARTING
    assert row.created_at == row.updated_at == 1000


def test_insert_twice_is_rejected():
    db.insert_sandbox(ID_A)
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_sandbox(ID_A)


def test_set_state_changes_state_and_updated_at_only(clock):
    db.insert_sandbox(ID_A)
    clock["now"] = 1005
    assert db.set_state(ID_A, SandboxState.RUNNING) is True
    row = db.get_sandbox(ID_A)
    assert row.state == SandboxState.RUNNING
    assert (row.created_at, row.updated_at) == (1000, 1005)


def test_set_state_on_unknown_sandbox_returns_false():
    assert db.set_state(ID_A, SandboxState.RUNNING) is False


# --- reads ------------------------------------------------------------------

def test_get_unknown_sandbox_returns_none():
    assert db.get_sandbox(ID_A) is None


def test_get_fills_slot_number_or_none():
    db.insert_sandbox(ID_A)
    db.insert_sandbox(ID_B)
    claim_slot(7, ID_A)
    assert db.get_sandbox(ID_A).n == 7
    assert db.get_sandbox(ID_B).n is None


def test_list_returns_every_sandbox_oldest_first_with_slots(clock):
    for offset, sandbox_id in enumerate([ID_B, ID_A, ID_C]):
        clock["now"] = 1000 + offset
        db.insert_sandbox(sandbox_id)
    claim_slot(3, ID_A)
    db.set_state(ID_C, SandboxState.DESTROYED)

    rows = db.list_sandboxes()
    assert [r.id for r in rows] == [ID_B, ID_A, ID_C]
    assert [r.n for r in rows] == [None, 3, None]


def test_list_live_skips_destroyed():
    db.insert_sandbox(ID_A)
    db.insert_sandbox(ID_B)
    db.set_state(ID_B, SandboxState.DESTROYED)
    assert [r.id for r in db.list_live_sandboxes()] == [ID_A]


def test_lists_are_empty_on_fresh_database():
    assert db.list_sandboxes() == []
    assert db.list_live_sandboxes() == []
