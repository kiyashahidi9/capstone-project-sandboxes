# Testing

Two checks. The suite runs on a laptop and does not boot a VM. The host check uses a tiny agent that prints step lines and exits, then confirms those lines were stored and that a quiet finished turn is paused for real.

## Suite

From `sbx/`:

```bash
.venv/bin/pytest -q
```

Pause and resume in these tests stand in for `helpers.pause` and `helpers.resume`. A passing suite means the beat decided to pause and called `pause`. It does not mean a Firecracker process was stopped.

## Sample agent

`examples/agent/replay_sample.py` reads the user message, prints five JSON lines, and exits. It does not call a model and it does not run a command in the sandbox.

The locked-down agent runs as `sbx-agent` and cannot read the repo. Copy the file to `/opt/agents` on the host:

```bash
sudo cp examples/agent/replay_sample.py /opt/agents/replay_sample.py
sudo chmod 644 /opt/agents/replay_sample.py
```

In the host `.env`, point the command at it and restart the API:

```bash
AGENT_CMD=/opt/agents/.venv/bin/python /opt/agents/replay_sample.py
```

Start the API from `sbx/` the usual way:

```bash
sudo .venv/bin/uvicorn api.main:app --host 127.0.0.1 --port 8000
```

`KEY` below is `SANDBOX_ADMIN_KEY` from `.env`.

## Replay

Create a sandbox, send one message, and read the events.

```bash
ID=$(curl -s -X POST localhost:8000/sandboxes -H "Authorization: Bearer $KEY" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
curl -s -X POST localhost:8000/sandboxes/$ID/messages -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -d '{"text":"do the thing"}'
curl -s localhost:8000/sandboxes/$ID/events -H "Authorization: Bearer $KEY"
```

The event types, in order, are `user_message`, `turn_started`, `llm_request`, `llm_response`, `tool_started`, `tool_completed`, `llm_response`, `turn_finished`. The second `llm_response` has `"text": "done"`. The `tool_completed` result is `exit_code` 0 and stdout `hi\n`. Nothing ran `echo hi` inside the VM. The sandbox is still `running`.

## Idleness

Leave that sandbox alone. Do not send another message. The finished turn is idle. The beat looks about every 5 seconds. It pauses only after the CPU samples have stayed under 2% of one core for 30 seconds, which is about 40 seconds after the turn finishes.

```bash
curl -s localhost:8000/sandboxes/$ID -H "Authorization: Bearer $KEY"
pgrep -af firecracker
```

The sandbox state goes to `pausing`, then `paused`. The Firecracker process for that sandbox is gone, and its snapshot files are on disk.

If it is still `running` after a minute, the VM's own CPU stayed at or above 2%. The turn was idle. The quiet period was not met, so pause did not run.
