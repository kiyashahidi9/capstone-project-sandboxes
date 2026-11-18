# Skateboard end-to-end test: every track on the GCP host

**What it tests**

- Part 1, Milestone 1: create, exec, files, destroy, auth.
- Part 2, Track C: pause and resume by hand.
- Part 3, Track A: the agent on the host, through the chat.
- Part 4, Track B: the sleep judge auto-pauses an idle sandbox, and a message wakes it.
- Part 5, Track B: agent steps are stored as events.
- Part 6, Track A: guardrails.

**What it doesn't test:** crash recovery, timings under load, many sandboxes at once.

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
git clone https://github.com/kiyashahidi9/capstone-project-sandboxes.git ~/capstone-project-sandboxes   # first time only
cd ~/capstone-project-sandboxes
git fetch && git checkout final-skateboard && git pull
cp .env.example .env                                               # first time only
python3 -c "import secrets; print(secrets.token_urlsafe(32))"      # first time only: the admin key
sudo nano .env      # set SANDBOX_ADMIN_KEY and OPENAI_API_KEY; leave AGENT_CMD as it is
cd sbx
chmod +x scripts/*
sudo scripts/setup_host.sh
sudo scripts/setup_agents.sh
pgrep -a firecracker; ip netns list; pgrep -af uvicorn
.venv/bin/pytest -q
```

- [ ] Both setup scripts show every line `[x]`.
- [ ] `pgrep` and `ip netns list` print nothing.
- [ ] pytest shows no `failed`.

**Laptop:**

```bash
git clone https://github.com/kiyashahidi9/capstone-project-sandboxes.git   # first time only
cd capstone-project-sandboxes
git fetch && git checkout final-skateboard && git pull
cp .env.example .env      # first time only; set SANDBOX_ADMIN_KEY to the host's value
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
watch_state() { for i in $(seq 18); do echo "$(date +%T) $(state)"; sleep 5; done; }
```

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

**VM-2:**

```bash
ID=$(api -X POST $API/sandboxes | jq -r .id)
IP=$(api $API/sandboxes/$ID | jq -r .ip)
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
api -X POST $API/sandboxes/$ID/pause | jq -c '{state, paused_at}'          # "paused"
pgrep -af "$ID" | grep firecracker || echo "no VM"                         # no VM
sudo ls -lh /var/sbx/$ID/snap/                                             # mem and vmstate
curl -s -m 3 http://$IP:3000/ || echo "unreachable"                        # unreachable
api -o /dev/null -w "%{http_code}\n" -X POST $API/sandboxes/$ID/pause      # 409
```

- [ ] Every line prints what its comment says.

Wait 2 minutes. Don't run `run`, `put` or `get` while you wait.

**VM-2:** resume.

```bash
api -X POST $API/sandboxes/$ID/resume | jq -c '{state, paused_at}'         # "running", null
curl -s http://$IP:3000/                                                   # ok <PID BEFORE>
get /root/notes.txt                                                        # before
run 'date -u'; date -u                                                     # match within a few seconds
api -o /dev/null -w "%{http_code}\n" -X POST $API/sandboxes/$ID/resume     # 409
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
pgrep -af "$ID" | grep firecracker || echo "no VM"           # no VM
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

## Part 7: Cleanup

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
