# Observability

See what an agent did in a sandbox, and why a turn failed, even after the agent or the VM is gone.

## How it works

Nothing new is stored. Everything is read from the existing `events` table in `state.db`. That table is on the host and never edited, and its rows stay after a sandbox is destroyed.

| Piece | Where | Does |
|-------|-------|------|
| Step events | Agent → runner | The agent prints `llm_request`, `llm_response`, `tool_started` and `tool_completed` lines ([agent contract](agent_contract.md)). The example agent now does this |
| Failure cause | `runner/runner.py` | A failed turn's `turn_failed` gets a `cause` |
| Turn summaries | `api/observability.py` | Groups events into turns and sums each one up |
| Endpoints | `api/main.py` | Read-only; also work for destroyed sandboxes |
| Timeline page | `dashboard/src/SandboxDetail.tsx` | Click a sandbox ID in the dashboard |

## Endpoints

| Method | Endpoint | Returns |
|--------|----------|---------|
| GET | `/sandboxes/{id}/turns` | One summary per turn: `status`, `reason`, `cause`, `exit_code`, `stderr_tail`, `last_action`, `unfinished_step`, `seconds_since_last_event` |
| GET | `/sandboxes/{id}/events?turn=N&after=S` | One turn's events, oldest first. 404 if there is no turn `N` |

## Choices

| Question | Choice | Why |
|----------|--------|-----|
| Store | The existing `events` table, not OpenTelemetry | A crash loses spans that haven't been exported yet, and that's exactly the moment we need |
| Session | One **turn** = one user message plus everything after it, until the next message | Turns already exist, so no schema change is needed. Turns count from 1 |
| Order | `seq` | Assigned by the database, so it's exact. `at` stays in whole seconds |
| Agent vs sandbox | On `exit` or `timeout`, the runner checks the VM: if it still answers, `cause` is `agent`, otherwise `sandbox` | Only the host can see both sides |
| Other causes | `paused` (a user or auto-pause), `host` (server restart, missing `AGENT_CMD`, runner error), `unknown` (the VM check failed) | Worked out from `reason` when no cause is stored |
| Unresponsive | A turn that's still working with no event for 5 minutes (`UNRESPONSIVE_AFTER`) | It's a warning. The 30-minute timeout still does the killing |
| Unfinished step | The newest `tool_started` with no `tool_completed`, or `llm_request` with no `llm_response` | Shows what the agent was doing when it died |
| Big payloads | Any string over 64,000 characters is cut, with `[truncated N characters]` added | Keeps one huge output from bloating `state.db`. Replay sees the cut text |
| Replay | Event shapes unchanged; `cause` is only added to `turn_failed` | Replay already reads these events and stops at `turn_failed` |

## Turn statuses

| Status | Meaning |
|--------|---------|
| `running` | Working, with a recent event |
| `unresponsive` | Working, but quiet for 5+ minutes |
| `finished` | `turn_finished` |
| `failed` | `turn_failed`; see `cause` |
| `paused` | A pause stopped it; not a failure |
| `incomplete` | No end recorded and not working. Shouldn't happen |

## Tests

`uv run pytest tests/test_observability.py tests/test_runner.py`
