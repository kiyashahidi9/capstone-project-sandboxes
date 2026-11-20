# Persistent Sandboxes

Persistent Firecracker microVM sandboxes for long-running coding agents, hosted on your own GCP project. All code lives in `sbx/`.

## 1. Set up the host (on your computer, at the repo root)

Install the [Google Cloud CLI](https://cloud.google.com/sdk/docs/install), push your branch (the VM pulls it), then:

```bash
gcloud auth login
gcloud config set project <your-project-id>   # billing must be on
sbx/scripts/install.sh                        # or: --agent ~/my-bot --cmd "python -m mybot"
```

It asks for your OpenAI key and takes ~15–20 min. **Save the `SANDBOX_ADMIN_KEY` it prints** — it's shown once. Safe to rerun.

## 2. Connect and chat (from `sbx/`)

```bash
cp ../.env.example ../.env    # set SANDBOX_ADMIN_KEY
uv sync
gcloud compute ssh sbx@sbx-node-1 --zone=us-east1-b -- -N -L 8000:localhost:8000   # terminal 2
uv run python -m client.client
```

Type a task at `>`. Sandboxes pause after ~30s idle and resume on your next message. `/quit` destroys the sandbox. To bring your own agent, see [agent_contract.md](sbx/docs/agent_contract.md).

## 3. Open the dashboard (from `sbx/dashboard/`)

Needs [Node.js](https://nodejs.org/) 18+, the SSH tunnel from step 2, and `SANDBOX_ADMIN_KEY` in the repo-root `.env`.

```bash
npm install
npm run dev      # open http://localhost:5173
```

See every sandbox and pause, resume or delete it. Click a sandbox ID to see its turns, each turn's **Timeline** (messages, LLM calls, tools, failures) and **Replay** (what the model was sent at each step). Restart `npm run dev` after changing `.env`; the admin key stays in the dev server and never reaches the browser. Details: [observability.md](sbx/docs/observability.md).

## 4. Run the tests (from `sbx/`, no VM needed)

```bash
uv run pytest
```

Stop/start the VM with `gcloud compute instances stop|start sbx-node-1 --zone=us-east1-b`; sandboxes come back paused. API logs: `sudo journalctl -u sbx-api -f` on the VM.
