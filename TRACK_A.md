# Track A: Milestone 2

The agent moves from the client onto the host. The host runs it once per message, records what it prints, and tracks whether it's busy, which is what pause and resume need later.

| Step | Status |
|------|--------|
| 2b Event log and messages | Done |
| 2c Agent contract | Done |
| 2d Example agent | Built; hand test pending |
| 2e Agent runner | Placeholder |
| 2f Guardrails | — |
| 2g Thin chat client | — |
| 2h Acceptance test | — |

## 2b: Event log and messages

**Database** (`db/schema.sql`)
- New `events` table: one row per thing that happened (`sandbox_id`, `seq`, `type`, `data` as JSON, `at`). `seq` counts 1, 2, 3, ... per sandbox.
- New `sandboxes.turn_state` column: `idle` or `working`.
- On an existing `state.db`, add the column once:
  ```bash
  sqlite3 /var/sbx/state.db "ALTER TABLE sandboxes ADD COLUMN turn_state TEXT NOT NULL DEFAULT 'idle' CHECK (turn_state IN ('idle', 'working'))"
  ```

**Endpoints** (`api/main.py`)

| Method | Endpoint | Does |
|--------|----------|------|
| POST | `/sandboxes/{id}/messages` `{"text": "..."}` | Sets the sandbox to `working`, saves a `user_message` event, starts the runner in the background, and answers `202 {"seq": N}`. 409 if a turn is already running |
| GET | `/sandboxes/{id}/events?after=N` | `{"events": [...]}`: every event with `seq > N`, oldest first |

`GET /sandboxes/{id}` now also shows `turn_state`.

**Notes**
- The turn is claimed with one atomic `UPDATE ... WHERE turn_state = 'idle'`, so two messages sent at once can't both start a turn.
- `runner/runner.py` is a placeholder until 2e. It immediately records `turn_failed` and sets the sandbox back to `idle`.
- Tests are in `tests/test_api.py`. Run them with `uv run pytest tests/test_api.py`.

## 2c: Agent contract

[`sbx/docs/agent_contract.md`](sbx/docs/agent_contract.md) tells a client how to hand us their agent: a command run once per message, with the message on stdin and progress on stdout. It ends the turn by exiting. 2d and 2e are built to it.

Decided beyond the spec:
- `SANDBOX_HISTORY` is a JSON list of earlier events, in the same shape `GET /events` returns.
- Only stdout becomes events. stderr is kept just for `turn_failed` (last 50 lines).
- Only a valid JSON object with `"type": "question"` is a question. Every other line is plain output.

## 2d: Example agent

**Agent:** `sbx/examples/agent/agent.py` (plus `tools.py`, moved from `client/`). It follows the contract: it reads the message from stdin and earlier turns from `SANDBOX_HISTORY`, prints each step, and exits 0 when done or 1 at the 50-step cap.

**SDK** (`sdk/sandbox.py`)
- `Sandbox(id, api, token=None)` sends `Authorization: Bearer <token>` when given a token.
- `Sandbox.from_env()` builds one from `SANDBOX_ID`, `SANDBOX_API` and `SANDBOX_TOKEN`.
- `pyproject.toml` now installs `sdk` into the venv, so agents can import it from any folder. Run `uv sync` once after pulling.

**Try it** (from `sbx/`, with the API running):
```bash
echo "create hello.py that prints hello, and run it" | \
  SANDBOX_ID=<id> SANDBOX_API=http://127.0.0.1:8000 SANDBOX_HISTORY=/dev/null \
  uv run python examples/agent/agent.py
```
