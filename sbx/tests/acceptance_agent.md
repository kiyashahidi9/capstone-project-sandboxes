# Milestone 1 acceptance test

A person runs this by hand. Every step must pass.

You use 3 terminals, all opened on your laptop:

| Terminal | Runs on | How to open it | Used for |
|---|---|---|---|
| **VM-1** | The VM | `gcloud compute ssh <vm> --zone=<zone>` | Setup, then the API server |
| **VM-2** | The VM | `gcloud compute ssh <vm> --zone=<zone> -- -L 8000:localhost:8000` | Checks (`curl`, `ls`, `pgrep`). Also tunnels port 8000 to your laptop, so keep it open. |
| **Laptop** | Your laptop | A normal terminal | The client |

Replace `<vm>` and `<zone>` with your VM's name and zone (e.g. `sbx-node-1`, `us-east1-b`).

## Before you start

**VM-1:** get the latest code and set up the host. On a fresh VM, first run `git clone https://github.com/kiyashahidi9/capstone-project-sandboxes.git ~/capstone-project-sandboxes`.

```bash
cd ~/capstone-project-sandboxes
git checkout 1HI-integrate && git pull
cd sbx
sudo scripts/setup_host.sh    # every checklist line must show [x]
pgrep firecracker             # must print nothing (no sandboxes running)
```

**Laptop:** get the same code, add your OpenAI key, and install dependencies.

```bash
cd capstone-project-sandboxes
git checkout 1HI-integrate && git pull
cp .env.example .env          # then set OPENAI_API_KEY=... in .env
cd sbx
uv sync
```

- [ ] `setup_host.sh` shows all `[x]`, `pgrep firecracker` prints nothing, and `.env` has your key.

## 1. Start the API

**VM-1** (from `sbx/`, leave running):

```bash
sudo .venv/bin/uvicorn api.main:app --host 127.0.0.1 --port 8000
```

## 2. Open VM-2 and start the client

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

- [ ] The client prints each step (`[run]`, `[read]`, `[write]`) and ends with `done`.

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

- [ ] The client ends with `done`.

**VM-2:**

```bash
curl http://<ip>:3000/health
```

- [ ] It shows the time.

## 6. Quit and check cleanup

**Laptop**, at the `>` prompt: `/quit`

**VM-2:**

```bash
ip netns list                  # nothing
pgrep firecracker              # nothing
find /sys/fs/cgroup/sandboxes -mindepth 1 -type d    # nothing (no sbx_<id> folders)
ls /var/sbx                    # only state.db (and state.db-wal / state.db-shm)
```

- [ ] All four match.

## 7. Record boot time

**VM-2**, 3 times:

```bash
curl -s -X POST localhost:8000/sandboxes            # note "id" and "boot_seconds"
curl -s -X DELETE localhost:8000/sandboxes/<id>
```

- [ ] Add the 3 `boot_seconds` values and their average to `NOTES.md` (Firecracker start to guest agent ready).

## 8. Stop

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
