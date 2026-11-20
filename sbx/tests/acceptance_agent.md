# Agent path acceptance test

A person who didn't write the code runs this by hand on a host set up with `install.sh`. Every step must pass. For a full from-scratch run (install, SDK, stream, bucket, reboot), use `sbx/Track C car end-to-end test - new client from scratch on GCP.md` instead.

The agent runs **on the host**: the laptop only runs a thin chat app, and the VM runs both the API (as the `sbx-api` service) and the agent.

You use 3 terminals, all opened on your laptop:

| Terminal | Runs on | How to open it | Used for |
|---|---|---|---|
| **VM-1** | The VM | `gcloud compute ssh sbx@<vm> --zone=<zone>` | The API's log, and the guardrail run |
| **VM-2** | The VM | `gcloud compute ssh sbx@<vm> --zone=<zone> -- -L 8000:localhost:8000` | Checks (`curl`, `ls`, `pgrep`). Also tunnels port 8000 to your laptop, so keep it open. |
| **Laptop** | Your laptop | A normal terminal | The chat app |

Replace `<vm>` and `<zone>` with your VM's name and zone (e.g. `sbx-node-1`, `us-east1-b`).

## Before you start

**Laptop:** set up the host from the `final-car` branch (see `readme/setup_README.md`). Save the admin key it prints.

```bash
git checkout final-car && git pull
sbx/scripts/install.sh
printf 'SANDBOX_ADMIN_KEY=%s\nSANDBOX_API=http://localhost:8000\n' "<the admin key>" > .env
cd sbx && uv sync
```

The laptop needs the admin key but no OpenAI key. The VM keeps it in a root-only `secrets.env`, and the API's key proxy adds it to the agent's model calls.

**VM-1:**

```bash
systemctl is-active sbx-api      # active
pgrep firecracker                # must print nothing (no sandboxes running)
sudo journalctl -u sbx-api -f    # leave this running to watch the API
```

- [ ] `install.sh` ends with `Done.`, the service is `active`, and `pgrep firecracker` prints nothing.

## 1. The API

It already runs as the `sbx-api` service, so there's nothing to start.

## 2. Open VM-2 and start the chat

**VM-2:** open it with the tunnel command from the table. Every request needs the admin key, so make a short helper, then check the API answers:

```bash
KEY=$(sudo grep '^SANDBOX_ADMIN_KEY=' ~/capstone-project-sandboxes/.env | cut -d= -f2-)
api() { curl -s -H "Authorization: Bearer $KEY" "$@"; }

api localhost:8000/sandboxes              # {"sandboxes":[...]}
curl -s localhost:8000/sandboxes          # {"detail":"Missing API key"}
```

- [ ] The first prints the sandbox list and the second is refused.

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
api localhost:8000/sandboxes/<id>/events    # user_message, turn_started, agent_output..., turn_finished
api localhost:8000/sandboxes/<id>           # "turn_state":"idle"
```

- [ ] Both match.

## 4. Check `/health`

**VM-2:**

```bash
api localhost:8000/sandboxes/<id>     # note the "ip" field, e.g. 10.201.0.1
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

## 6. Guardrails

A fake agent tries to break out of the lockdown. Every attempt must fail.

**VM-1:** stop the log (Ctrl+C) and the service, then start the API by hand with the guardrail agent instead of the example agent (from `~/capstone-project-sandboxes/sbx`):

```bash
sudo systemctl stop sbx-api
sudo env AGENT_CMD="/opt/agents/.venv/bin/python /opt/agents/guardrail_agent.py"   .venv/bin/uvicorn api.main:app --host 127.0.0.1 --port 8000
```

**VM-2** (from `sbx/`):

```bash
sudo bash scripts/check_guardrails.sh
```

It prints one line per check and ends with `all checks passed`:

- [ ] `PASS` its own token works on its own sandbox.
- [ ] `PASS` exec on another sandbox with its token → 403.
- [ ] `PASS` DELETE its own sandbox → 403.
- [ ] `PASS` call the API with no key → 401.
- [ ] `PASS` the metadata server is blocked.
- [ ] `PASS` cat the `.env` file → permission denied.
- [ ] `PASS` whoami prints `sbx-agent`.

**VM-1:** stop the hand-started API (Ctrl+C), then start the service again: `sudo systemctl start sbx-api`.

## 7. Pause and resume

- [ ] Leave the sandbox idle until it auto-pauses (about 30 seconds after the agent's turn ends; the chat shows `[paused]` with your next message), then send a follow-up. It resumes and carries on.

## 8. Quit and check cleanup

**Laptop**, at the `>` prompt: `/quit`

- [ ] It prints `sandbox sbx_<id> destroyed`.

**VM-2:**

```bash
ip netns list                  # nothing
pgrep firecracker              # nothing
pgrep -u sbx-agent             # nothing (no agent left running)
find /sys/fs/cgroup/sandboxes -mindepth 1 -type d    # nothing (no sbx_<id> folders)
ls /var/sbx                    # only state.db (and state.db-wal / state.db-shm)
```

- [ ] All five match.

## 9. Stop

Nothing to stop: the API keeps running as a service. **VM-1:** Ctrl+C to leave the log. **VM-2:** `exit`.

## Done when

- [ ] Someone who didn't write the code runs this on a host set up with `install.sh` and every box is ticked.
