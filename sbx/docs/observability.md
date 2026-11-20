# Observability

See what an agent did in a sandbox, why a turn failed, and exactly what the model was sent at each step, even after the agent or the VM is gone.

## How it works

Nothing new is stored. Everything is read from the existing `events` table in `state.db`. That table is on the host and never edited, and its rows stay after a sandbox is destroyed.

| Piece | Where | Does |
|-------|-------|------|
| Step events | Agent → runner | The agent prints `llm_request`, `llm_response`, `tool_started` and `tool_completed` lines ([agent contract](agent_contract.md)). The example agent does this |
| Shared history | `sdk/history.py` | Rebuilds the chat from events. The example agent builds its context with it, and so does replay |
| Agent version | `runner/runner.py` | `turn_started` stores `agent_cmd` and `agent_hash`, for the team only |
| Failure cause | `runner/runner.py` | A failed turn's `turn_failed` gets a `cause` |
| Turns and replay | `api/observability.py` | Groups events into turns, sums each one up, and replays its model calls |
| Endpoints | `api/main.py` | Read-only; also work for destroyed sandboxes |
| Pages | `dashboard/src/SandboxDetail.tsx`, `Replay.tsx` | Click a sandbox ID, pick a turn, then **Timeline** or **Replay** |

## Endpoints

| Method | Endpoint | Returns |
|--------|----------|---------|
| GET | `/sandboxes/{id}/turns` | One summary per turn: `message` (what the user asked), `status`, `reason`, `cause`, `exit_code`, `stderr_tail`, `last_action`, `unfinished_step`, `seconds_since_last_event` |
| GET | `/sandboxes/{id}/events?turn=N&after=S` | One turn's events, oldest first. 404 if there is no turn `N` |
| GET | `/sandboxes/{id}/replay?turn=N` | One step per model call: the `messages` sent (system prompt first), the `response`, each tool's `result`, and `matches_record`. 404 if there is no turn `N` |

## Replay

Replay rebuilds what the model was sent at each step, from events alone. It never calls the model or runs a tool.

- **One version of history:** the agent builds its chat with `chat_from_events` and adds every reply and result in their recorded form. Replay rebuilds with the same function, so it shows exactly what the model got, across turns too.
- **`matches_record`:** compares the rebuild with the messages the agent recorded in `llm_request`. `false` means the record was cut (64,000 characters) or the agent doesn't use `sdk/history.py`. `null` means the agent recorded no messages.
- **Turns without steps:** an agent that doesn't print steps has its `agent_output` lines used as its replies in later turns, and its own turns have no replay steps.

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
| Big payloads | Any string over 64,000 characters is cut, with `[truncated N characters]` added | Keeps one huge output from bloating `state.db`. Replay sees the cut text, and `matches_record` turns `false` |
| Message format | OpenAI's chat format | It's what the model gets, so replay can show it as is |
| Tool `result` | Exactly what the model was given: the example agent's reads and writes give `{"path", "content"}` | Replay must rebuild the model's input, so what's stored and what's sent must be the same |
| A call left unanswered | Gets `error: the turn ended before this tool finished` in later turns | OpenAI refuses a chat with an unanswered tool call |
| Agent version | `agent_cmd` plus a 12-digit hash of the files in the agent's folder (e.g. `/opt/agents`) | Shows when a different agent ran. The SDK in the agent's venv isn't part of the hash |
| Host-only fields | `agent_cmd` and `agent_hash` stay in `state.db` but are left out of every API response and the agent's history file (`HOST_ONLY_FIELDS`) | They show host paths, and clients have no use for them. Read them with `sqlite3` on the host |

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

`uv run pytest tests/test_observability.py tests/test_replay.py tests/test_agent_state.py tests/test_runner.py`

`test_replay.py` runs the real example agent through the runner with a fake model that logs what it receives, and checks replay rebuilds exactly that at every step.
