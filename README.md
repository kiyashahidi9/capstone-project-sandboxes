# Persistent Sandboxes

Persistent Firecracker microVM sandboxes for long-running coding agents, hosted on your own GCP project. All code lives in `sbx/`.

## 1. Set up the host (on your computer, at the repo root)

Install the [Google Cloud CLI](https://cloud.google.com/sdk/docs/install), push your branch (the VM pulls it), then:

```bash
gcloud auth login
gcloud config set project <your-project-id>   # billing must be on
sbx/scripts/install.sh                        # or: --agent ~/my-bot --cmd "python -m mybot"
```

It asks for the example agent's OpenAI key and takes ~15–20 min. The key lands in a root-only `secrets.env` on the VM; the agent never sees it, because its model calls go through the API's key proxy. Your own agent can use OpenAI or Anthropic: put its keys in `secrets.env` in its folder ([agent_contract.md](sbx/docs/agent_contract.md#your-keys)), and replace them later with `install.sh --update-secrets`. **Save the `SANDBOX_ADMIN_KEY` it prints** — it's shown once. Safe to rerun.

## 2. Connect and chat (from `sbx/`)

```bash
cp ../.env.example ../.env    # set SANDBOX_ADMIN_KEY
uv sync
gcloud compute ssh sbx@sbx-node-1 --zone=us-east1-b -- -N -L 8000:localhost:8000   # terminal 2
uv run python -m client.client
```

Type a task at `>`. Sandboxes pause after ~30s idle and resume on your next message. `/quit` destroys the sandbox. To bring your own agent, see [agent_contract.md](sbx/docs/agent_contract.md).

## 3. Run the tests (from `sbx/`, no VM needed)

```bash
uv run pytest
```

Stop/start the VM with `gcloud compute instances stop|start sbx-node-1 --zone=us-east1-b`; sandboxes come back paused. API logs: `sudo journalctl -u sbx-api -f` on the VM.
