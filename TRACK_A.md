# Track A: Milestone 2

The agent moves from the client onto the host. The host runs it once per message, records what it prints, and tracks whether it's busy, which is what pause and resume need later.

| Step | Status |
|------|--------|
| 2b Event log and messages | Done |
| 2c Agent contract | Done |
| 2d Example agent | Done; ran on the host through the chat |
| 2e Agent runner | Done; ran on the host (before 2f) |
| 2f Guardrails | Built; host check pending |
| 2g Thin chat client | Done; ran on the host |
| 2h Acceptance test | Checklist written; needs a run by a teammate |

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
- `pyproject.toml` now installs `sdk` into the venv, so agents can import it from any folder.

**Try it** on the host, with the API running. This makes a sandbox, runs the agent on one task, then destroys the sandbox:
```bash
bash sbx/scripts/try_agent.sh "create hello.py that prints hello, and run it"
```

## 2e: Agent runner

`sbx/runner/runner.py` runs the agent for each message and records the turn. `sbx/locks.py` gives each sandbox one lock, shared with pause and resume.

| Function | Does |
|----------|------|
| `run_turn(id)` | Takes the lock, records `turn_started`, runs `AGENT_CMD` with the contract's inputs, turns each stdout line into an event, then records `turn_finished` or `turn_failed` and sets `idle` |
| `stop_turn(id)` | For pause: kills the agent, records `turn_failed` (`paused`), and returns once the sandbox is idle. Call it before taking the lock |
| `recover_interrupted_turns()` | On server start: any sandbox still `working` gets `turn_failed` (`server restarted`) and goes back to `idle` |

**Setup** (in `.env`, see `.env.example`):
- `AGENT_CMD=.venv/bin/python examples/agent/agent.py` (relative to `sbx/`)
- `AGENT_TURN_TIMEOUT=1800` is optional. Past it, the agent and anything it started are killed (`timeout`).

`turn_failed` has a `reason`: `exit`, `timeout`, `paused`, `server restarted`, or a startup error. When the agent ran, it also has `exit_code` and `stderr_tail`, the last 50 lines of stderr.

Tests: `uv run pytest tests/test_runner.py` uses tiny fake agents.

## 2g: Thin chat client

`sbx/client/client.py` is now a thin chat app with no agent and no AI key inside. It creates a sandbox, sends each message with `POST /messages`, and polls `GET /events` every second until the turn ends:

```
sandbox sbx_58e4... ready
> create hello.py that prints hello, and run it
[write] /root/hello.py
[run] python hello.py
      hello
I created hello.py and ran it.
done
> /quit
sandbox sbx_58e4... destroyed
```

A question shows as `[waiting on you] ...`, and a failed turn as `error: <reason>` with the end of the agent's stderr. Run it from `sbx/` with `uv run python -m client.client`.

Also:
- The SDK gained `send_message(text)` and `events(after)`.
- `DELETE /sandboxes/{id}` now stops a running turn first.

Tests: `tests/test_client.py` runs the chat end to end (client → API → runner → fake agent), with only the VM faked.

## 2h: Acceptance test

[`sbx/tests/acceptance_agent.md`](sbx/tests/acceptance_agent.md) is the Milestone 2 checklist: it sets up the host's `.env`, then runs clone, install, dev server, `/health` and a follow-up through the thin chat, followed by the cleanup checks. The guardrail (2f) and pause/resume (Track C) steps are marked ⏸ until those are built. Someone who didn't write the code runs it on a fresh host.

## 2f: Guardrails

The client's agent can now only reach the AI and its own sandbox.

| Guardrail | How |
|-----------|-----|
| Every request needs a key | `api/auth.py`: the admin key (`SANDBOX_ADMIN_KEY`) may call everything. No key or a wrong key gets 401 |
| One sandbox only | Each turn gets a fresh token (`SANDBOX_TOKEN`) that only works for exec and files on its own sandbox, and is dropped when the turn ends. Anything else gets 403 |
| Locked-down process | `runner.py` starts the agent with `systemd-run` as `sbx-agent`, with the spec's limits (read-only filesystem, `/home` hidden, 2 GB, metadata server blocked) |
| Secrets | The agent gets only `OPENAI_API_KEY`, through a root-only env file instead of the command line. `.env` is root only |

**Host setup:** `sudo scripts/setup_agents.sh` creates `sbx-agent`, installs the agent and its own venv in `/opt/agents`, and makes `.env` root only. Then set `AGENT_CMD=/opt/agents/.venv/bin/python /opt/agents/agent.py`.

**Check it:** start the API with the guardrail agent, then run `sudo bash scripts/check_guardrails.sh` (step 6 of the acceptance test).

**For development only:** `AGENT_ISOLATION=none` runs the agent without `systemd-run`. The tests use it.

**Known gap:** all agents run as the same `sbx-agent` user, so two turns running at once could read each other's token from `/proc`. Fixing it means a separate user per turn (systemd's `DynamicUser=yes`).
