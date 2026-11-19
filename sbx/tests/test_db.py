import re
import sqlite3
from contextlib import closing

import pytest

import host_manager
from api import db
from api.models import EventType, SandboxState


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
    """Gives a sandbox a network slot the way host_manager.py does."""
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

def test_schema_file_matches_host_manager_slots_schema():
    assert slots_table_sql(db.SCHEMA_PATH.read_text()) == slots_table_sql(host_manager.SLOTS_SCHEMA)


def test_create_tables_is_safe_to_run_twice():
    db.create_tables()


OLD_SANDBOXES_TABLE = """
CREATE TABLE sandboxes (
    id          TEXT PRIMARY KEY,
    state       TEXT NOT NULL CHECK (state IN ('starting', 'running', 'destroyed')),
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);
"""


def use_old_sandboxes_table(rows):
    """Swaps in the pre-4c sandboxes table holding the given (id, state, created_at, updated_at) rows."""
    with closing(db.connect()) as conn:
        conn.execute("DROP TABLE sandboxes")
        conn.execute(OLD_SANDBOXES_TABLE)
        conn.executemany("INSERT INTO sandboxes VALUES (?, ?, ?, ?)", rows)


def table_sql(table):
    """Gives a table's definition as stored in the state database."""
    with closing(db.connect()) as conn:
        (sql,) = conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (table,)).fetchone()
    return sql


def test_rebuilt_table_is_the_one_in_schema_file():
    fresh = table_sql("sandboxes")
    use_old_sandboxes_table([])

    db.create_tables()

    assert table_sql("sandboxes") == fresh


def test_rebuild_leaves_no_temporary_table():
    use_old_sandboxes_table([(ID_A, "running", 1000, 1000)])

    db.create_tables()

    with closing(db.connect()) as conn:
        names = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "sandboxes_old" not in names


def test_create_tables_rebuilds_an_old_table_and_keeps_its_rows():
    use_old_sandboxes_table([(ID_A, "running", 1000, 1001), (ID_B, "destroyed", 1002, 1003)])

    db.create_tables()

    rows = db.list_sandboxes()
    assert [(r.id, r.state, r.created_at, r.updated_at) for r in rows] == [
        (ID_A, SandboxState.RUNNING, 1000, 1001),
        (ID_B, SandboxState.DESTROYED, 1002, 1003),
    ]
    assert all(r.snapshot_dir is None and r.paused_at is None for r in rows)


def test_create_tables_after_a_rebuild_changes_nothing():
    use_old_sandboxes_table([(ID_A, "running", 1000, 1001)])
    db.create_tables()
    db.transition(ID_A, [SandboxState.RUNNING], SandboxState.PAUSED, snapshot_dir="/snap", paused_at=1002)
    before = (table_sql("sandboxes"), db.list_sandboxes())

    db.create_tables()

    assert (table_sql("sandboxes"), db.list_sandboxes()) == before


# --- merging with another branch's columns ----------------------------------

TRACK_A_TABLE = """
CREATE TABLE sandboxes (
    id          TEXT PRIMARY KEY,
    state       TEXT NOT NULL CHECK (state IN ('starting', 'running', 'destroyed')),
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL,
    turn_state  TEXT NOT NULL DEFAULT 'idle'
);
"""


TRACK_C_TABLE = """
CREATE TABLE sandboxes (
    id            TEXT PRIMARY KEY,
    state         TEXT NOT NULL CHECK (state IN ('starting', 'running', 'pausing', 'paused', 'resuming', 'destroyed')),
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL,
    snapshot_dir  TEXT,
    paused_at     INTEGER
);
"""


def use_sandboxes_table(create_sql: str, insert_sql: str) -> None:
    """Swaps in a sandboxes table as an older branch made it, with one row."""
    with closing(db.connect()) as conn:
        conn.execute("DROP TABLE sandboxes")
        conn.execute(create_sql)
        conn.execute(insert_sql, (ID_A,))


def column_values(column):
    """Gives one column's value for every sandbox, by id."""
    with closing(db.connect()) as conn:
        return dict(conn.execute(f"SELECT id, {column} FROM sandboxes").fetchall())


def test_merge_keeps_track_a_column_values():
    use_sandboxes_table(TRACK_A_TABLE, "INSERT INTO sandboxes VALUES (?, 'running', 1000, 1000, 'working')")

    db.create_tables()

    assert column_values("turn_state") == {ID_A: "working"}
    assert db.set_state(ID_A, SandboxState.PAUSED) is True


def test_merge_keeps_track_c_column_values():
    use_sandboxes_table(TRACK_C_TABLE, "INSERT INTO sandboxes VALUES (?, 'paused', 1000, 1001, '/snap', 1002)")

    db.create_tables()

    row = db.get_sandbox(ID_A)
    assert (row.state, row.snapshot_dir, row.paused_at) == (SandboxState.PAUSED, "/snap", 1002)
    assert column_values("turn_state") == {ID_A: "idle"}


def test_comment_only_change_to_schema_does_not_rebuild(tmp_path, monkeypatch):
    before = table_sql("sandboxes")
    path = tmp_path / "schema.sql"
    path.write_text(db.SCHEMA_PATH.read_text().replace("-- unix seconds; NULL unless paused", "-- reworded"))
    monkeypatch.setattr(db, "SCHEMA_PATH", path)

    db.create_tables()

    assert table_sql("sandboxes") == before


def test_rebuilt_table_accepts_the_pause_states():
    use_old_sandboxes_table([(ID_A, "running", 1000, 1000)])
    db.create_tables()

    for state in (SandboxState.PAUSING, SandboxState.PAUSED, SandboxState.RESUMING):
        assert db.set_state(ID_A, state) is True
        assert db.get_sandbox(ID_A).state == state


def test_table_still_rejects_unknown_states():
    db.insert_sandbox(ID_A)
    with closing(db.connect()) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE sandboxes SET state = 'sleeping' WHERE id = ?", (ID_A,))


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


def make_sandbox(sandbox_id, state):
    """Records a sandbox and moves it straight to the given state."""
    db.insert_sandbox(sandbox_id)
    db.set_state(sandbox_id, state)


def test_transition_from_an_allowed_state_changes_it(clock):
    make_sandbox(ID_A, SandboxState.RUNNING)
    clock["now"] = 1005

    assert db.transition(ID_A, [SandboxState.RUNNING], SandboxState.PAUSING) is True

    row = db.get_sandbox(ID_A)
    assert row.state == SandboxState.PAUSING
    assert row.updated_at == 1005


def test_transition_from_a_wrong_state_changes_nothing(clock):
    make_sandbox(ID_A, SandboxState.PAUSED)
    clock["now"] = 1005

    assert db.transition(ID_A, [SandboxState.RUNNING], SandboxState.PAUSING) is False

    row = db.get_sandbox(ID_A)
    assert row.state == SandboxState.PAUSED
    assert row.updated_at == 1000


def test_transition_accepts_any_of_several_from_states():
    make_sandbox(ID_A, SandboxState.PAUSED)
    assert db.transition(ID_A, [SandboxState.RUNNING, SandboxState.PAUSED], SandboxState.DESTROYED) is True
    assert db.get_sandbox(ID_A).state == SandboxState.DESTROYED


def test_transition_on_unknown_sandbox_returns_false():
    assert db.transition(ID_A, [SandboxState.RUNNING], SandboxState.PAUSING) is False


def test_only_the_first_of_two_identical_transitions_wins():
    make_sandbox(ID_A, SandboxState.RUNNING)
    assert db.transition(ID_A, [SandboxState.RUNNING], SandboxState.PAUSING) is True
    assert db.transition(ID_A, [SandboxState.RUNNING], SandboxState.PAUSING) is False


def test_transition_sets_and_clears_the_pause_fields(clock):
    make_sandbox(ID_A, SandboxState.PAUSING)

    db.transition(ID_A, [SandboxState.PAUSING], SandboxState.PAUSED, snapshot_dir="/var/sbx/x/snap", paused_at=1000)
    paused = db.get_sandbox(ID_A)
    db.transition(ID_A, [SandboxState.PAUSED], SandboxState.RESUMING)
    resuming = db.get_sandbox(ID_A)
    db.transition(ID_A, [SandboxState.RESUMING], SandboxState.RUNNING, snapshot_dir=None, paused_at=None)
    running = db.get_sandbox(ID_A)

    assert (paused.snapshot_dir, paused.paused_at) == ("/var/sbx/x/snap", 1000)
    assert (resuming.snapshot_dir, resuming.paused_at) == ("/var/sbx/x/snap", 1000)
    assert (running.snapshot_dir, running.paused_at) == (None, None)


def test_transition_rejects_a_field_it_does_not_know():
    make_sandbox(ID_A, SandboxState.RUNNING)
    with pytest.raises(ValueError):
        db.transition(ID_A, [SandboxState.RUNNING], SandboxState.PAUSING, created_at=0)
    assert db.get_sandbox(ID_A).state == SandboxState.RUNNING


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


# --- event listeners --------------------------------------------------------

def test_a_new_event_wakes_that_sandbox_with_its_seq():
    updates = db.listen(ID_A)
    try:
        seq = db.append_event(ID_A, EventType.USER_MESSAGE, {"text": "hi"})
        assert updates.get(timeout=1) == seq
        assert updates.empty()
    finally:
        db.unlisten(ID_A, updates)


def test_an_event_for_another_sandbox_does_not_wake_this_one():
    updates = db.listen(ID_A)
    try:
        db.append_event(ID_B, EventType.USER_MESSAGE, {"text": "hi"})
        assert updates.empty()
    finally:
        db.unlisten(ID_A, updates)


def test_destroying_a_sandbox_wakes_its_listener_with_nothing():
    db.insert_sandbox(ID_A)
    updates = db.listen(ID_A)
    try:
        assert db.set_state(ID_A, SandboxState.DESTROYED) is True
        assert updates.get(timeout=1) is None
    finally:
        db.unlisten(ID_A, updates)


def test_unlisten_drops_later_events():
    updates = db.listen(ID_A)
    db.unlisten(ID_A, updates)
    db.append_event(ID_A, EventType.USER_MESSAGE, {"text": "hi"})
    assert updates.empty()
