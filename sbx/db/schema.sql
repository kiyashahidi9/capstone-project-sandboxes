-- Creates the tables in state.db; safe to run again: sqlite3 /var/sbx/state.db ".read db/schema.sql"

PRAGMA journal_mode = WAL;   -- reads don't wait for writes

-- Which sandbox holds which network slot; owned by node_agent.py and must match its SLOTS_SCHEMA.
CREATE TABLE IF NOT EXISTS slots (
    n           INTEGER PRIMARY KEY CHECK (n BETWEEN 1 AND 250),  -- slot number, one row per taken slot
    sandbox_id  TEXT NOT NULL UNIQUE                              -- a sandbox holds at most one slot
);

-- One row per sandbox the API created; owned by the API server, rows kept after destroy.
-- SQLite can't change a CHECK in place, so changing the states means rebuilding this table:
-- rebuild_sandboxes_table() in api/db.py renames the old table, creates this one, copies the rows, drops the old.
-- It reads this statement from here, so this is the only definition of the table.
CREATE TABLE IF NOT EXISTS sandboxes (
    id            TEXT PRIMARY KEY,   -- sbx_<32 hex>, matches slots.sandbox_id
    state         TEXT NOT NULL CHECK (state IN ('starting', 'running', 'pausing', 'paused', 'resuming', 'destroyed')),
    created_at    INTEGER NOT NULL,   -- unix seconds
    updated_at    INTEGER NOT NULL,   -- unix seconds, changes with state
    turn_state    TEXT NOT NULL DEFAULT 'idle' CHECK (turn_state IN ('idle', 'working')),  -- is the agent running a turn right now
    snapshot_dir  TEXT,               -- host folder holding the snapshot; NULL unless paused
    paused_at     INTEGER             -- unix seconds; NULL unless paused
);

-- The whole conversation of each sandbox, user messages included; owned by the API server, never edited.
CREATE TABLE IF NOT EXISTS events (
    sandbox_id  TEXT NOT NULL,
    seq         INTEGER NOT NULL,   -- 1, 2, 3, ... per sandbox
    type        TEXT NOT NULL,      -- user_message | agent_output | question | llm_request | llm_response | tool_started | tool_completed | turn_started | turn_finished | turn_failed | paused | resumed
    data        TEXT NOT NULL,      -- JSON
    at          INTEGER NOT NULL,   -- unix seconds
    PRIMARY KEY (sandbox_id, seq)
);
