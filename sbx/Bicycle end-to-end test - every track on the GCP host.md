# Bicycle end-to-end test: every track on the GCP host

The whole system on real Firecracker, from the `track-c-bicycle` branch. About 75 minutes.

| Part | Tests |
|---|---|
| 1 | Create, exec, files, destroy, auth |
| 2 | Pause and resume by hand |
| 3 | The agent through the chat |
| 4 | Auto-pause and wake-up |
| 5 | Recorded agent steps |
| 6 | Guardrails |
| 7 | Recovery after a crash (new) |
| 8 | Pause and resume timings (new) |

## Terminals

| Name | Open it with |
|---|---|
| **VM-1** | `gcloud compute ssh sbx-node-1 --zone=us-east1-b` |
| **VM-2** | `gcloud compute ssh sbx-node-1 --zone=us-east1-b -- -L 8000:localhost:8000` |
| **Laptop** | A normal terminal |

---

## Part 0: Setup

**Laptop:**

```bash
gcloud compute instances start sbx-node-1 --zone=us-east1-b
```

**VM-1:**

```bash
cd ~/capstone-project-sandboxes
git fetch && git checkout track-c-bicycle && git pull
cd sbx
sudo scripts/setup_host.sh          # rebuilds the base image with the sandbox manager
sudo scripts/setup_agents.sh
pgrep -a firecracker; ip netns list; pgrep -af uvicorn
.venv/bin/pytest -q
```

- [ ] Both setup scripts show every line `[x]`, including `sandbox manager in the base image`.
- [ ] If `setup_host.sh` prints `these VM disks were made before this install`, run the `rm` it suggests.
- [ ] `pgrep` and `ip netns list` print nothing.
- [ ] pytest shows no `failed`.

**Laptop:**

```bash
cd capstone-project-sandboxes
git fetch && git checkout track-c-bicycle && git pull
cd sbx
uv sync
```

- [ ] `uv sync` finishes without errors.

---

## Part 1: Sandboxes (Milestone 1)

**VM-1** (from `sbx/`, leave it running):

```bash
sudo .venv/bin/uvicorn api.main:app --host 127.0.0.1 --port 8000
```

**VM-2:** paste these helpers (again after any reconnect):

```bash
cd ~/capstone-project-sandboxes/sbx
KEY=$(sudo grep '^SANDBOX_ADMIN_KEY=' ../.env | cut -d= -f2-)
API=localhost:8000
api()   { curl -s -H "Authorization: Bearer $KEY" "$@"; }
state() { api $API/sandboxes/$ID | jq -c '{state, turn_state, paused_at}'; }
run()   { api -X POST $API/sandboxes/$ID/exec -H "Content-Type: application/json" \
            -d "$(jq -n --arg c "$1" '{cmd: $c}')" | jq -r '.stdout // .detail'; }
bg()    { api -X POST $API/sandboxes/$ID/exec -H "Content-Type: application/json" \
            -d "$(jq -n --arg c "$1" '{cmd: $c, background: true}')" | jq -c .; }
put()   { api -X PUT "$API/sandboxes/$ID/files?path=$1" --data-binary "$2" | jq -c .; }
get()   { api "$API/sandboxes/$ID/files?path=$1"; echo; }
vm()    { pgrep -f "$ID/fc.sock" >/dev/null && echo "VM running" || echo "no VM"; }
dbstate() { sudo .venv/bin/python -c "import sqlite3, sys; print(sqlite3.connect('/var/sbx/state.db').execute('SELECT state FROM sandboxes WHERE id = ?', (sys.argv[1],)).fetchone()[0])" $ID; }
watch_state() { for i in $(seq 18); do echo "$(date +%T) $(state)"; sleep 5; done; }
```

`dbstate` reads the state straight from the database, so it works while the API is down (Part 7).

**VM-2:**

```bash
curl -s $API/sandboxes; echo                                    # {"detail":"Missing API key"}
curl -s -H "Authorization: Bearer wrong" $API/sandboxes; echo   # {"detail":"Invalid API key"}

A=$(api -X POST $API/sandboxes | jq -r .id)
B=$(api -X POST $API/sandboxes | jq -r .id)
api $API/sandboxes | jq -c '.sandboxes[] | {id, state, ip}'     # A and B running, different IPs

ID=$A
run 'whoami'                                 # root
put /root/hello.txt 'hello from A'           # {"ok":true}
get /root/hello.txt                          # hello from A

ID=$B
run 'cat /root/hello.txt || echo "no such file"'    # no such file

api -X DELETE $API/sandboxes/$A; echo        # {"ok":true}
api -X DELETE $API/sandboxes/$A; echo        # {"ok":true}
api -X DELETE $API/sandboxes/$B; echo        # {"ok":true}
pgrep -a firecracker; ip netns list          # nothing
```

- [ ] Every line prints what its comment says.

---

## Part 2: Pause and resume by hand (Track C)

**VM-2:** create a sandbox and run the sandbox manager's own checklist inside it.

```bash
ID=$(api -X POST $API/sandboxes | jq -r .id)
IP=$(api $API/sandboxes/$ID | jq -r .ip)
scripts/install_sandbox_manager.sh test ${IP##*.}
```

- [ ] Every line is `[x]`, including the `pause and resume (4b)` lines (`/sync` and `/clock`), and it ends with `OK`.

**VM-2:** start a dev server and write a file.

```bash
put /root/server.js 'require("http").createServer((q, s) => s.end("ok " + process.pid + "\n")).listen(3000)'
bg 'node /root/server.js'
sleep 2
curl -s http://$IP:3000/                     # ok <PID>: write down PID BEFORE
put /root/notes.txt 'before'
sleep 60; state                              # still "running"
```

- [ ] `curl` prints `ok <PID>`, and the state is still `running` after a minute.

**VM-2:** pause.

```bash
time api -X POST $API/sandboxes/$ID/pause | jq -c '{state, paused_at}'     # "paused", in about 10–25 s
vm                                                                         # no VM
sudo ls -lh /var/sbx/$ID/snap/                                             # mem and vmstate
curl -s -m 3 http://$IP:3000/ || echo "unreachable"                        # unreachable
api -o /dev/null -w "%{http_code}\n" -X POST $API/sandboxes/$ID/pause      # 409
```

- [ ] Every line prints what its comment says.

Wait 2 minutes. Don't run `run`, `put` or `get` while you wait, because they resume a paused sandbox.

**VM-2:** resume.

```bash
time api -X POST $API/sandboxes/$ID/resume | jq -c '{state, paused_at}'    # "running", null, in about 2 s
curl -s http://$IP:3000/                                                   # ok <PID BEFORE>
get /root/notes.txt                                                        # before
run 'date -u'; date -u                                                     # match within a few seconds
api -o /dev/null -w "%{http_code}\n" -X POST $API/sandboxes/$ID/resume     # 409
```

- [ ] Every line prints what its comment says.
- [ ] If the VM's time is about 2 minutes behind, `/clock` didn't run: look for `could not set the clock` in VM-1.

**VM-2:** a resume sent during a pause waits for the pause to finish.

```bash
api -X POST $API/sandboxes/$ID/pause > /dev/null &
sleep 2; state                                                             # "pausing"
time api -X POST $API/sandboxes/$ID/resume | jq -r .state                  # running, after about 10–15 s
curl -s http://$IP:3000/                                                   # ok <PID BEFORE>
```

- [ ] Every line prints what its comment says.

**VM-2:** exec resumes a paused sandbox, and destroy removes a paused one.

```bash
api -X POST $API/sandboxes/$ID/pause | jq -c '{state}'      # "paused"
run 'cat /root/notes.txt'                                   # before
state                                                       # "running"
api -X POST $API/sandboxes/$ID/pause | jq -c '{state}'      # "paused"
api -X DELETE $API/sandboxes/$ID; echo                      # {"ok":true}
sudo ls /var/sbx                                            # only state.db files
pgrep -a firecracker; ip netns list                         # nothing
```

- [ ] Every line prints what its comment says.

---

## Part 3: The agent through the chat (Track A)

**Laptop** (from `sbx/`):

```bash
uv run python -m client.client
```

- [ ] It prints `sandbox sbx_<id> ready`.

**VM-2:**

```bash
ID=sbx_<id>
IP=$(api $API/sandboxes/$ID | jq -r .ip)
```

**Laptop**, at the `>` prompt:

```
Clone https://github.com/kiyashahidi9/example-express-app into /root/app, install dependencies, start the dev server in the background on port 3000, and add a /health route that returns ok.
```

- [ ] The chat prints `[run]`, `[read]` and `[write]` steps and ends with `done`.

**VM-2:**

```bash
api $API/sandboxes/$ID/events | jq -r '.events[].type' | sort | uniq -c   # user_message, turn_started, agent_output, turn_finished
state                                                                     # "turn_state":"idle"
curl -s http://$IP:3000/health; echo                                      # ok
```

- [ ] Every line prints what its comment says.

**Laptop**, right away:

```
Change /health to also return the current time.
```

**VM-2:**

```bash
curl -s http://$IP:3000/health; echo         # shows the time
run 'pgrep -f node'                          # write down SERVER PID
```

- [ ] The chat ends with `done`, and `/health` shows the time.

---

## Part 4: Auto-pause and wake-up (Track B)

Don't type in the chat. **VM-2**, right after the chat prints `done`:

```bash
watch_state
```

- [ ] Within about 60 s, the state goes `running` → `pausing` → `paused`.

**VM-2:**

```bash
vm                                                          # no VM
sudo ls -lh /var/sbx/$ID/snap/                              # mem and vmstate
curl -s -m 3 http://$IP:3000/health || echo "unreachable"   # unreachable
```

- [ ] Every line prints what its comment says.

**Laptop:**

```
Write the word "after" to /root/notes.txt and show me the file.
```

- [ ] The chat ends with `done` and shows `after`.

**VM-2:**

```bash
curl -s http://$IP:3000/health; echo         # answers
run 'pgrep -f node'                          # same SERVER PID
run 'date -u'; date -u                       # match within a few seconds
watch_state                                  # back to "paused" within about 60 s
```

- [ ] Every line prints what its comment says.

**Laptop:** `/quit`

- [ ] It prints `sandbox sbx_<id> destroyed`.

---

## Part 5: Recorded agent steps (Track B)

**VM-2:**

```bash
sudo install -o root -g sbx-agent -m 0640 examples/agent/replay_sample.py /opt/agents/
```

**VM-1:** Ctrl+C, then:

```bash
sudo env AGENT_CMD="/opt/agents/.venv/bin/python /opt/agents/replay_sample.py" \
  .venv/bin/uvicorn api.main:app --host 127.0.0.1 --port 8000
```

**VM-2:**

```bash
ID=$(api -X POST $API/sandboxes | jq -r .id)
api -X POST $API/sandboxes/$ID/messages -H "Content-Type: application/json" -d '{"text":"do the thing"}'; echo   # {"seq":1}
sleep 3
api $API/sandboxes/$ID/events | jq -r '.events[].type'
api $API/sandboxes/$ID/events | jq -c '.events[] | select(.type=="tool_completed") | .data'
watch_state
api -X DELETE $API/sandboxes/$ID; echo
```

- [ ] The types are, in order: `user_message`, `turn_started`, `llm_request`, `llm_response`, `tool_started`, `tool_completed`, `llm_response`, `turn_finished`.
- [ ] `tool_completed` has `"tool_call_id":"call_1"`, `exit_code` 0 and stdout `hi\n`.
- [ ] The state goes to `paused` within about 60 s.

---

## Part 6: Guardrails (Track A)

**VM-1:** Ctrl+C, then:

```bash
sudo env AGENT_CMD="/opt/agents/.venv/bin/python /opt/agents/guardrail_agent.py" \
  .venv/bin/uvicorn api.main:app --host 127.0.0.1 --port 8000
```

**VM-2:**

```bash
sudo bash scripts/check_guardrails.sh
```

- [ ] All 7 lines say `PASS`, and it ends with `all checks passed`.

---

## Part 7: Recovery after a crash (Track C, bicycle)

Each step breaks something, restarts the API, and checks what startup cleaned up.

**VM-1:** Ctrl+C, then start the normal API again and leave it running:

```bash
sudo .venv/bin/uvicorn api.main:app --host 127.0.0.1 --port 8000
```

"Restart the API" below means: in **VM-1**, run the same command again. If VM-1 is still running it, press Ctrl+C first.

### 7a. A crash in the middle of a pause

**VM-2:**

```bash
ID=$(api -X POST $API/sandboxes | jq -r .id)
api -X POST $API/sandboxes/$ID/pause > /dev/null &
sleep 3; sudo pkill -9 -f "uvicorn api.main:app"      # a hard crash: Ctrl+C would let the pause finish
dbstate                                               # pausing
```

- [ ] `dbstate` prints `pausing`: the crash left it stuck.

Restart the API, then on **VM-2**:

```bash
state                                                 # "running" or "paused", never "pausing"
run 'echo ok'                                         # ok (resumes it first if it was paused)
api -X DELETE $API/sandboxes/$ID; echo                # {"ok":true}
```

- [ ] Every line prints what its comment says.

### 7b. A VM that died while the API was down

**VM-2:**

```bash
ID=$(api -X POST $API/sandboxes | jq -r .id)
vm                                                    # VM running
sudo pkill -9 -f "$ID/fc.sock"; sleep 1; vm           # no VM
```

Restart the API, then on **VM-2**:

```bash
state                                                 # "destroyed"
sudo ls /var/sbx                                      # only state.db files
ip netns list                                         # nothing
```

- [ ] Every line prints what its comment says.

### 7c. A paused sandbox survives a restart

**VM-2:**

```bash
ID=$(api -X POST $API/sandboxes | jq -r .id)
put /root/notes.txt 'kept'
api -X POST $API/sandboxes/$ID/pause | jq -c '{state}'   # "paused"
```

Restart the API, then on **VM-2**:

```bash
state                                                 # still "paused"
get /root/notes.txt                                   # kept (resumes it first)
state                                                 # "running"
```

- [ ] Every line prints what its comment says. A paused sandbox has no VM on purpose, so startup must not destroy it.

### 7d. A paused sandbox with a broken snapshot

**VM-2:**

```bash
api -X POST $API/sandboxes/$ID/pause | jq -c '{state}'   # "paused"
sudo rm /var/sbx/$ID/snap/vmstate                       # break the snapshot
```

Restart the API, then on **VM-2**:

```bash
state                                                 # "destroyed"
sudo ls /var/sbx                                      # only state.db files
```

- [ ] Every line prints what its comment says.

### 7e. A leftover sandbox the API doesn't know about

**VM-2:** make a sandbox behind the API's back, using the host manager directly.

```bash
sudo .venv/bin/python -c "import host_manager; print(host_manager.create_sandbox().id)"   # write down: LEFTOVER
sudo ls /var/sbx                                      # LEFTOVER's folder is there
```

Restart the API, then on **VM-2**:

```bash
sudo ls /var/sbx                                      # only state.db files
pgrep -a firecracker; ip netns list                   # nothing
```

- [ ] Every line prints what its comment says.

---

## Part 8: Pause and resume timings (Track C, bicycle)

This calls the host manager directly, not the API, so stop the API first. **VM-1:** Ctrl+C, then:

```bash
sudo .venv/bin/python scripts/bench_pause.py          # about 10 minutes
sudo .venv/bin/python scripts/bench_pause.py --cold   # about 10 minutes; drops the file cache before each resume
```

- [ ] Both print a table for 512, 1024 and 2048 MiB, with no errors.
- [ ] The numbers are close to the table in `readme/Track C README.md` (Bicycle stage section): pause about 10 / 13 / 23 s, warm resume about 0.15 s, cold resume about 2 s at every size.
- [ ] `pgrep -a firecracker` prints nothing afterwards (every run destroys its sandbox).

---

## Part 9: Cleanup

**VM-1:** start the API once more, so startup clears anything left over:

```bash
sudo .venv/bin/uvicorn api.main:app --host 127.0.0.1 --port 8000
```

**VM-2:**

```bash
api $API/sandboxes | jq -r '.sandboxes[].state' | sort | uniq -c     # only "destroyed"
pgrep -a firecracker; pgrep -u sbx-agent; ip netns list               # nothing
find /sys/fs/cgroup/sandboxes -mindepth 1 -type d                     # nothing
sudo ls /var/sbx                                                      # only state.db files
```

- [ ] Every line prints what its comment says.

**VM-1:** Ctrl+C. If it hangs, from **VM-2**: `sudo pkill -f "uvicorn api.main:app"`.

**Laptop:**

```bash
gcloud compute instances stop sbx-node-1 --zone=us-east1-b
```
