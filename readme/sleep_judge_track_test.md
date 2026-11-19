# sleep_judge_track_test

This branch gives a client a live event stream for one sandbox, so the client's own orchestrator can follow a turn without polling. Roost still does not choose the next sandbox. It stores the events and sends them. Workflow state stays on the client.

Replayability for observability is not part of this branch. The event log is stored, and this branch does not rebuild an agent run from it, play it back, or export a trace.

Idle pause is unchanged. A heartbeat thread still samples CPU every 5 seconds and pauses a sandbox that has been waiting or idle and quiet. That loop is not the client stream.

## The stream

`GET /sandboxes/{id}/events/stream?after={seq}`

Send the admin key as `Authorization: Bearer`. The body is one JSON object per line (`seq`, `type`, `data`, `at`). A blank line once a second keeps the connection open. The server sends the backlog after `seq`, then each new event, until the sandbox is destroyed. Closing the connection unsubscribes. A dropped connection is resumed by calling the same URL with the last `seq` received.

This is not browser Server-Sent Events. `EventSource` cannot set the `Authorization` header, so the SDK reads the lines itself (`Sandbox.subscribe`).

The chat client holds that subscription for the life of the sandbox. It prints `agent_output`, `question`, `turn_finished`, `turn_failed`, `paused`, and `resumed`. An orchestrator on the same stream can act on `turn_started`, `turn_finished`, and `turn_failed`. `paused` and `resumed` describe the machine.

`GET /sandboxes/{id}/events?after={seq}` is still a one-shot snapshot.

## Unit tests

On a laptop, from `sbx/`:

```bash
uv sync
uv run pytest
```

Tests that boot a real Firecracker VM skip here. They need root and `/dev/kvm`. The run should show passed tests and those skips, and no failures.

On the GCP host the same suite is the integration run. `sudo` is required: `.env` is root-only after setup, and the Firecracker tests need root and `/dev/kvm`.

```bash
cd ~/capstone-project-sandboxes
git fetch && git checkout final-car && git pull
cd sbx
sudo scripts/setup_host.sh
sudo scripts/setup_agents.sh
sudo systemctl stop sbx-api
sudo .venv/bin/pytest -q
sudo systemctl start sbx-api
```

This run should not skip `tests/test_host_manager.py`. No failures. The full host checklist (install, SDK, chat, the stream, pause, the bucket, reboot) is `sbx/Track C car end-to-end test - new client from scratch on GCP.md`. This branch is merged into `final-car`.

## Stream test on the GCP host

The host is `sbx-node-1` in `us-east1-b`. This test signs a client up for the event stream, has the agent do three short turns, and saves the JSON the client received for each turn.

**Laptop:**

```bash
gcloud compute instances start sbx-node-1 --zone=us-east1-b
```

The API runs on the VM as the `sbx-api` service, so nothing needs starting there.

**Laptop**, a second terminal, with the tunnel. Leave it open.

```bash
gcloud compute ssh sbx@sbx-node-1 --zone=us-east1-b -- -N -L 8000:localhost:8000
```

**Laptop**, from `sbx/`. The repo-root `.env` needs the same `SANDBOX_ADMIN_KEY` as the VM, and `SANDBOX_API=http://localhost:8000`. No OpenAI key on the laptop. The host already has one, and the agent runs there.

```bash
uv sync
uv run python scripts/test_check_sandbox_subscription.py
```

The client creates a sandbox, subscribes for the life of that sandbox, then sends three tasks:

1. Create `/root/count.txt` containing `1`.
2. Read that file and reply with only its contents.
3. Change the file so it contains `2`.

Each event is printed as one JSON line as it arrives. When a turn ends (`turn_finished` or `turn_failed`), that turn's events are written to:

```text
stream-log/<sandbox id>/turn-1.json
stream-log/<sandbox id>/turn-2.json
stream-log/<sandbox id>/turn-3.json
```

Each file is a JSON list, in the order the client received the events, and the last event of a successful turn is `turn_finished`. Open those files to see what the client got after each turn. The script then destroys the sandbox, which ends the subscription.

When finished, stop the VM:

```bash
gcloud compute instances stop sbx-node-1 --zone=us-east1-b
```
