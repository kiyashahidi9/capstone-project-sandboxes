-- Creates the tables in state.db; safe to run again: sqlite3 /var/sbx/state.db ".read db/schema.sql"

PRAGMA journal_mode = WAL;   -- reads don't wait for writes

-- Which sandbox holds which network slot; owned by node_agent.py and must match its SLOTS_SCHEMA.
CREATE TABLE IF NOT EXISTS slots (
    n           INTEGER PRIMARY KEY CHECK (n BETWEEN 1 AND 250),  -- slot number, one row per taken slot
    sandbox_id  TEXT NOT NULL UNIQUE                              -- a sandbox holds at most one slot
);

-- One row per sandbox the API created; owned by the API server, rows kept after destroy.
CREATE TABLE IF NOT EXISTS sandboxes (
    id          TEXT PRIMARY KEY,   -- sbx_<32 hex>, matches slots.sandbox_id
    state       TEXT NOT NULL CHECK (state IN ('starting', 'running', 'destroyed')),  -- adding a state later means rebuilding this table
    created_at  INTEGER NOT NULL,   -- unix seconds
    updated_at  INTEGER NOT NULL    -- unix seconds, changes with state
);
