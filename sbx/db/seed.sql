-- Sample data for a local test database only; never run on the host's /var/sbx/state.db.

-- Stop at the first error so nothing is half-loaded.
.bail on

BEGIN;

-- Seed only if both tables are empty (decided once here; temp table disappears when sqlite3 exits).
CREATE TEMP TABLE seed_check AS
    SELECT NOT EXISTS (SELECT 1 FROM sandboxes) AND NOT EXISTS (SELECT 1 FROM slots) AS empty;

-- Two running, one booting with a slot, one booting without a slot, one destroyed.
INSERT INTO sandboxes (id, state, created_at, updated_at)
SELECT * FROM (VALUES
    ('sbx_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'running',   1794355200, 1794355202),
    ('sbx_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', 'running',   1794355260, 1794355262),
    ('sbx_cccccccccccccccccccccccccccccccc', 'starting',  1794355320, 1794355320),
    ('sbx_dddddddddddddddddddddddddddddddd', 'starting',  1794355380, 1794355380),
    ('sbx_eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee', 'destroyed', 1794351600, 1794355100)
)
WHERE (SELECT empty FROM seed_check);

-- Only sandboxes that have claimed a slot appear here.
INSERT INTO slots (n, sandbox_id)
SELECT * FROM (VALUES
    (1, 'sbx_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'),
    (2, 'sbx_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'),
    (3, 'sbx_cccccccccccccccccccccccccccccccc')
)
WHERE (SELECT empty FROM seed_check);

-- Say whether the seed ran.
SELECT CASE WHEN empty THEN 'seeded' ELSE 'skipped: tables not empty' END FROM seed_check;

COMMIT;
