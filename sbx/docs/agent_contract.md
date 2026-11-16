# Agent contract

How to hand us your agent. Your agent is a **command**. We run it once for each message the user sends.

| | |
|---|---|
| **How we start it** | The command in `AGENT_CMD` (e.g. `python /opt/agents/agent.py`), once per message, as a fresh process |
| **What it gets** | The new message on **stdin**, plus the environment variables below |
| **How it works on the sandbox** | Our SDK (`sdk/sandbox.py`) or plain HTTP to `SANDBOX_API` with `Authorization: Bearer $SANDBOX_TOKEN`. Only the exec and files routes, and only for its own sandbox |
| **How it reports progress** | Every line it prints on **stdout** becomes an event the user sees |
| **How it ends a turn** | It exits. Exit code `0` = finished; anything else = failed (we save the last 50 lines of stderr) |
| **Limits** | 30 min per turn, 2 GB memory, no root, no access to the host's files |

## Environment

| Variable | Value |
|----------|-------|
| `SANDBOX_ID` | The sandbox to work in, e.g. `sbx_58e4...` |
| `SANDBOX_API` | `http://127.0.0.1:8000` |
| `SANDBOX_TOKEN` | A token that only works for this sandbox, for this turn |
| `SANDBOX_HISTORY` | Path to a JSON file of earlier events (below) |
| `OPENAI_API_KEY` | The AI key from the host's `.env` |

## History

`SANDBOX_HISTORY` is a JSON list of every earlier event, oldest first, in the same shape `GET /sandboxes/{id}/events` returns. It doesn't include the current message, which is on stdin:

```json
[
  {"seq": 1, "type": "user_message", "data": {"text": "create hello.py"}, "at": 1794816000},
  {"seq": 2, "type": "turn_started", "data": {}, "at": 1794816000},
  {"seq": 3, "type": "agent_output", "data": {"text": "[write] /root/hello.py"}, "at": 1794816003},
  {"seq": 4, "type": "turn_finished", "data": {"exit_code": 0}, "at": 1794816009}
]
```

To rebuild the conversation, use the `user_message` events (what the user said) and the `agent_output` and `question` events (what you said).

## Output

- **stdout:** each line becomes an `agent_output` event, `{"text": "<the line>"}`.
- **A question:** print one JSON line `{"type": "question", "text": "..."}`, then exit 0. The user's answer comes back as the next message, with this turn in the history. Only a valid JSON object with `"type": "question"` counts. Any other line is plain output.
- **stderr:** never shown to the user. Use it for debug logs. If the turn fails, its last 50 lines are saved in the `turn_failed` event.

## Example

```python
import sys
from sdk.sandbox import Sandbox

message = sys.stdin.read()
sandbox = Sandbox.from_env()

print("[run] echo hello", flush=True)
result = sandbox.exec("echo hello")
print(result["stdout"].strip(), flush=True)
```

Print with `flush=True` (or run Python with `-u`) so the user sees each line as it happens.

## Why a command, not a Python import

The agent can be written in any language, a crash can't take the API server down, and the process exiting tells us exactly when the turn ended.
