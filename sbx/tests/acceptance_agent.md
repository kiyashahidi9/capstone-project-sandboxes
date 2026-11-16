# Milestone 2 acceptance test

A person who didn't write the code runs this by hand on a fresh host. Every step must pass.

In Milestone 2 the agent runs **on the host**: the laptop only runs a thin chat app, and the VM runs both the API server and the agent.

You use 3 terminals, all opened on your laptop:

| Terminal | Runs on | How to open it | Used for |
|---|---|---|---|
| **VM-1** | The VM | `gcloud compute ssh <vm> --zone=<zone>` | Setup, then the API server |
| **VM-2** | The VM | `gcloud compute ssh <vm> --zone=<zone> -- -L 8000:localhost:8000` | Checks (`curl`, `ls`, `pgrep`). Also tunnels port 8000 to your laptop, so keep it open. |
| **Laptop** | Your laptop | A normal terminal | The chat app |

Replace `<vm>` and `<zone>` with your VM's name and zone (e.g. `sbx-node-1`, `us-east1-b`).

Steps marked **⏸ not built yet** depend on 2f (guardrails) or Track C (pause and resume). Skip them until those land, and leave their boxes unticked.

## Before you start

**VM-1:** get the latest code and set up the host. On a fresh VM, first run `git clone https://github.com/kiyashahidi9/capstone-project-sandboxes.git ~/capstone-project-sandboxes`.

```bash
cd ~/capstone-project-sandboxes
git checkout track-a && git pull
cp .env.example .env          # then set OPENAI_API_KEY=... in .env; keep AGENT_CMD as it is
cd sbx
sudo scripts/setup_host.sh    # every checklist line must show [x]
pgrep firecracker             # must print nothing (no sandboxes running)
```

**Laptop:** get the same code and install dependencies. The laptop no longer needs an OpenAI key.

```bash
cd capstone-project-sandboxes
git checkout track-a && git pull
cd sbx
uv sync
```

- [ ] `setup_host.sh` shows all `[x]`, `pgrep firecracker` prints nothing, and the VM's `.env` has `OPENAI_API_KEY` and `AGENT_CMD`.

## 1. Start the API

**VM-1** (from `sbx/`, leave running):

```bash
sudo .venv/bin/uvicorn api.main:app --host 127.0.0.1 --port 8000
```

## 2. Open VM-2 and start the chat

**VM-2:** open it with the tunnel command from the table, then check the API answers.

```bash
curl -s localhost:8000/sandboxes          # {"sandboxes":[...]}
```

**Laptop** (from `sbx/`):

```bash
uv run python -m client.client
```

- [ ] It prints `sandbox sbx_<id> ready`. Copy the id.

## 3. First task

**Laptop**, at the `>` prompt:

```
Clone https://github.com/kiyashahidi9/example-express-app, install dependencies, start the dev server in the background on port 3000, and add a /health route that returns ok.
```

- [ ] The chat prints each step (`[run]`, `[read]`, `[write]`) as it happens and ends with `done`.

**VM-2:** the conversation is stored on the host.

```bash
curl -s localhost:8000/sandboxes/<id>/events    # user_message, turn_started, agent_output..., turn_finished
curl -s localhost:8000/sandboxes/<id>           # "turn_state":"idle"
```

- [ ] Both match.

## 4. Check `/health`

**VM-2:**

```bash
curl -s localhost:8000/sandboxes/<id>     # note the "ip" field, e.g. 10.201.0.1
curl http://<ip>:3000/health
```

- [ ] It returns `ok`.

## 5. Follow-up task

**Laptop**, at the `>` prompt:

```
Change /health to also return the current time.
```

- [ ] The chat ends with `done`, without being told where the app is. It knows from the history the host passed it.

**VM-2:**

```bash
curl http://<ip>:3000/health
```

- [ ] It shows the time.

## 6. Guardrails ⏸ not built yet (2f)

Run a turn with a fake agent that tries each of these. Every one must fail:

- [ ] Call exec on another sandbox with its token → 403.
- [ ] DELETE its own sandbox → 403.
- [ ] Call the API with no key → 401.
- [ ] `curl http://169.254.169.254` → blocked.
- [ ] `cat` the `.env` file → permission denied.
- [ ] `whoami` prints `sbx-agent`.

## 7. Pause and resume ⏸ not built yet (Track C)

- [ ] Leave the sandbox idle until it auto-pauses, then send a follow-up. It resumes and carries on.
- [ ] `/pause`, `/resume`, `/keep-awake` and `/status` each work.

## 8. Quit and check cleanup

**Laptop**, at the `>` prompt: `/quit`

- [ ] It prints `sandbox sbx_<id> destroyed`.

**VM-2:**

```bash
ip netns list                  # nothing
pgrep firecracker              # nothing
pgrep -f examples/agent        # nothing (no agent left running)
find /sys/fs/cgroup/sandboxes -mindepth 1 -type d    # nothing (no sbx_<id> folders)
ls /var/sbx                    # only state.db (and state.db-wal / state.db-shm)
```

- [ ] All five match.

## 9. Stop

**VM-1:** Ctrl+C. If it doesn't stop, press Ctrl+C again (the first one waits for open requests to finish).

Still running, or VM-1 won't respond? **VM-2:**

```bash
sudo pkill -f "uvicorn api.main:app"      # add -9 if it's still running after a few seconds
pgrep -af uvicorn                         # nothing
```

If VM-1 ignores all keys, the SSH session is frozen: press Enter, then type `~.` to close it.

**VM-2:** `exit`.

## Done when

- [ ] Someone who didn't write the code runs this on a fresh host and every box is ticked.
