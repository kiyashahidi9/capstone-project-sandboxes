# Agent contract

How to hand us your agent. Your agent is a **command**. We run it once for each message the user sends.

| | |
|---|---|
| **How we start it** | The command in `AGENT_CMD` (e.g. `python /opt/agents/agent.py`), once per message, as a fresh process |
| **What it gets** | The new message on **stdin**, plus the environment variables below |
| **How it works on the sandbox** | Our SDK (`sdk/sandbox.py`) or plain HTTP to `SANDBOX_API` with `Authorization: Bearer $SANDBOX_TOKEN`. Only the exec and files routes, and only for its own sandbox |
| **How it reports progress** | Every line it prints on **stdout** becomes an event the user sees |
| **How it ends a turn** | It exits. Exit code `0` = finished; anything else = failed (we save the last 50 lines of stderr) |
| **Limits** | 30 min per turn, 2 GB memory, one CPU, no root, no access to the host's files. It runs as `sbx-agent` with a read-only filesystem and its own private `/tmp`. Do real work in the sandbox, not on the host |

## Environment

| Variable | Value |
|----------|-------|
| `SANDBOX_ID` | The sandbox to work in, e.g. `sbx_58e4...` |
| `SANDBOX_API` | `http://127.0.0.1:8000` |
| `SANDBOX_TOKEN` | A token that only works for exec and files on this sandbox, and only during this turn. Anything else gets 403 |
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

## Steps

Print one JSON line per step, before the next side effect. Each line is stored as its own event. The `type` field selects the event. The other fields are the event data. A tool is one action the agent can take, whether that action is a sandbox file or an external call such as mail.

Order for one model round:

```json
{"type": "llm_request", "model": "gpt-4.1", "system": "...", "tools": [], "messages": []}
{"type": "llm_response", "tool_calls": [{"tool_call_id": "call_1", "name": "run_command", "arguments": {"cmd": "echo hi"}}]}
{"type": "tool_started", "tool_call_id": "call_1", "name": "run_command", "arguments": {"cmd": "echo hi"}}
{"type": "tool_completed", "tool_call_id": "call_1", "result": {"exit_code": 0, "stdout": "hi\n", "stderr": ""}}
```

A text answer is `{"type": "llm_response", "text": "..."}`. A write stores `{"path": "...", "content": "..."}` as its `result`. `llm_request` carries the model, the system prompt, the tool schemas, and the messages. It does not carry an API key.

The steps are stored so a run can be inspected, or played back later from `user_message`, `llm_response` and `tool_completed` without calling the model or running the tool again. Nothing plays them back yet. The sleep judge uses `tool_started` and `tool_completed`: a tool call with no completion counts as working, so the sandbox isn't paused mid-tool. Plain text stays `agent_output`.

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
