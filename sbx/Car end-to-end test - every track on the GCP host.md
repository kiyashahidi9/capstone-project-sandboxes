# Car end-to-end test: every track on the GCP host

The whole system on real Firecracker and a real bucket, from the `final-bicycle` branch. About 80 minutes, 20 of them waiting for the install. Best run by a teammate who didn't write `install.sh`.

| Part | Tests |
|---|---|
| 0 | Install with `install.sh`, then rerun it (6a) |
| 1 | Create, exec, files, destroy, auth, metadata server blocked |
| 2 | Pause and resume by hand, upload to the bucket (7a) |
| 3 | The agent through the chat |
| 4 | Auto-pause and wake-up |
| 5 | Recorded agent steps |
| 6 | Guardrails |
| 7 | Recovery after a crash, restore from the bucket (7b) |
| 8 | Swap in your own agent (6a) |
| 9 | Stop and start the whole VM (6c) |
| 10 | Auto-delete forgotten sandboxes (6b) |
| 11 | Cleanup |

**Before you start**
- `gcloud` installed and logged in (`gcloud auth login`).
- A GCP project with billing on. A **new** project is best: it proves the installer works from nothing.
- An OpenAI API key.
- The repo cloned on your laptop. `install.sh` installs the commit you're on, so it must be pushed.

## Terminals

| Name | Open it with | Used for |
|---|---|---|
| **Laptop** | A normal terminal, at the repo root | `install.sh`, the chat, `gcloud` |
| **VM-1** | `gcloud compute ssh sbx@$VM --zone=$ZONE`, then `sudo journalctl -u sbx-api -f` | Watching the API's log |
| **VM-2** | `gcloud compute ssh sbx@$VM --zone=$ZONE -- -L 8000:localhost:8000` | Running commands; also the tunnel for the chat |

Always log in as **`sbx@`**: that's the account the installer uses, so the repo is at `~/capstone-project-sandboxes`.

---

## Part 0: Install (6a)

**Laptop:**

```bash
git fetch && git checkout final-bicycle && git pull
git status --short                       # nothing: uncommitted changes are NOT installed
gcloud config set project <your-project-id>
export VM=sbx-node-1 ZONE=us-east1-b     # if the project already has an old sbx-node-1, use a new name, e.g. VM=sbx-e2e
VM_NAME=$VM ZONE=$ZONE sbx/scripts/install.sh
```

It asks once for your OpenAI key (typing is hidden), then takes about 15–20 minutes.

- [ ] Steps `1.` to `13.` run without `error:`, and the setup scripts' checklists inside them show every line `[x]`.
- [ ] It ends with `Done.` and shows `export SANDBOX_ADMIN_KEY=...`. **Save the key.**

**Laptop:** save the key for the chat (Part 3) and check that a rerun changes nothing.

```bash
echo "SANDBOX_ADMIN_KEY=<the key>" >> .env      # repo root; .env is gitignored
VM_NAME=$VM ZONE=$ZONE sbx/scripts/install.sh   # the rerun
```

- [ ] Every step says `already exists`, including `.env (keeping its admin key)`. It doesn't ask for the OpenAI key.
- [ ] It says `nothing changed, so the running API was left alone`, and shows **no** new key.

Now open **VM-1** and **VM-2**.

**VM-2:** check the service, then run the unit tests on the host.

```bash
systemctl is-active sbx-api; systemctl is-enabled sbx-api   # active, enabled
cd ~/capstone-project-sandboxes/sbx
sudo systemctl stop sbx-api
sudo .venv/bin/pytest -q                  # sudo: .env is root only, and the real Firecracker tests need root
sudo systemctl start sbx-api
```

- [ ] pytest shows no `failed` and no `skipped`.

**Laptop:** the API through the tunnel (VM-2 must be open).

```bash
curl -s -H "Authorization: Bearer <the key>" localhost:8000/sandboxes; echo   # {"sandboxes":[]}
```

- [ ] It prints `{"sandboxes":[]}`.

---

## Part 1: Sandboxes (Milestone 1)

**VM-2:** paste these helpers (again after any reconnect):

```bash
cd ~/capstone-project-sandboxes/sbx
KEY=$(sudo grep '^SANDBOX_ADMIN_KEY=' ../.env | cut -d= -f2-)
BUCKET=$(sudo grep '^SNAPSHOT_BUCKET=' ../.env | cut -d= -f2-)
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
col()   { sudo .venv/bin/python -c "import sqlite3, sys; print(sqlite3.connect('/var/sbx/state.db').execute(f'SELECT {sys.argv[2]} FROM sandboxes WHERE id = ?', (sys.argv[1],)).fetchone()[0])" $ID $1; }
upload()      { col upload_status; }
wait_upload() { for i in $(seq 60); do [ "$(upload)" = done ] && break; sleep 5; done; upload; }
restart_api() { sudo systemctl restart sbx-api; until curl -s -o /dev/null $API/sandboxes; do sleep 1; done; }
watch_state() { for i in $(seq 18); do echo "$(date +%T) $(state)"; sleep 5; done; }
```

`col` reads a column straight from the database (`upload_status` isn't in the API). `restart_api` restarts the service and waits until it answers.

**VM-2:**

```bash
curl -s $API/sandboxes; echo                                    # {"detail":"Missing API key"}
curl -s -H "Authorization: Bearer wrong" $API/sandboxes; echo   # {"detail":"Invalid API key"}

A=$(api -X POST $API/sandboxes | jq -r .id)
B=$(api -X POST $API/sandboxes | jq -r .id)
api $API/sandboxes | jq -c '.sandboxes[] | select(.state != "destroyed") | {id, state, ip}'   # A and B running, different IPs

ID=$A
run 'whoami'                                 # root
put /root/hello.txt 'hello from A'           # {"ok":true}
get /root/hello.txt                          # hello from A
run 'curl -s -m 3 -H "Metadata-Flavor: Google" http://169.254.169.254/ || echo blocked'   # blocked

ID=$B
run 'cat /root/hello.txt || echo "no such file"'    # no such file

api -X DELETE $API/sandboxes/$A; echo        # {"ok":true}
api -X DELETE $API/sandboxes/$A; echo        # {"ok":true}
api -X DELETE $API/sandboxes/$B; echo        # {"ok":true}
pgrep -a firecracker; ip netns list          # nothing
```

- [ ] Every line prints what its comment says. `blocked` means code in a sandbox can't get the VM's bucket credentials.

---

## Part 2: Pause and resume by hand, upload to the bucket (Track C, 7a)

**VM-2:** create a sandbox and run the sandbox manager's own checklist inside it.

```bash
ID=$(api -X POST $API/sandboxes | jq -r .id)
IP=$(api $API/sandboxes/$ID | jq -r .ip)
scripts/install_sandbox_manager.sh test ${IP##*.}
```

- [ ] Every line is `[x]`, including the `pause and resume (4b)` lines, and it ends with `OK`.

**VM-2:** start a dev server, write a file, pause.

```bash
put /root/server.js 'require("http").createServer((q, s) => s.end("ok " + process.pid + "\n")).listen(3000)'
bg 'node /root/server.js'
sleep 2
curl -s http://$IP:3000/                     # ok <PID>: write down PID BEFORE
put /root/notes.txt 'before'

time api -X POST $API/sandboxes/$ID/pause | jq -c '{state, paused_at}'     # "paused", in about 10–25 s
vm                                                                         # no VM
curl -s -m 3 http://$IP:3000/ || echo "unreachable"                        # unreachable
api -o /dev/null -w "%{http_code}\n" -X POST $API/sandboxes/$ID/pause      # 409
wait_upload                                                                # done (can take a few minutes: ~5 GiB)
gcloud storage ls -r $BUCKET/$ID/                                          # one folder with mem, rootfs.ext4, vmstate, complete
```

- [ ] Every line prints what its comment says.

Wait 1 minute. Don't run `run`, `put` or `get` while you wait: they resume a paused sandbox.

**VM-2:** resume.

```bash
time api -X POST $API/sandboxes/$ID/resume | jq -c '{state, paused_at}'    # "running", null, in about 2 s
curl -s http://$IP:3000/                                                   # ok <PID BEFORE>
get /root/notes.txt                                                        # before
run 'date -u'; date -u                                                     # match within a few seconds
api -o /dev/null -w "%{http_code}\n" -X POST $API/sandboxes/$ID/resume     # 409
```

- [ ] Every line prints what its comment says.

**VM-2:** a resume sent during a pause waits, exec resumes, and destroy removes the bucket copies too.

```bash
api -X POST $API/sandboxes/$ID/pause > /dev/null &
sleep 2; state                                              # "pausing"
time api -X POST $API/sandboxes/$ID/resume | jq -r .state   # running, after about 10–15 s
api -X POST $API/sandboxes/$ID/pause | jq -c '{state}'      # "paused"
run 'cat /root/notes.txt'                                   # before
state                                                       # "running"
api -X DELETE $API/sandboxes/$ID; echo                      # {"ok":true}
gcloud storage ls $BUCKET/$ID/                              # an error saying it matched no objects
sudo ls /var/sbx                                            # only state.db files
```

- [ ] Every line prints what its comment says.

---

## Part 3: The agent through the chat (Track A)

**Laptop** (VM-2 open for the tunnel):

```bash
(cd sbx && uv sync && uv run python -m client.client)
```

- [ ] It prints `sandbox sbx_<id> ready`.

**VM-2:**

```bash
ID=sbx_<id>
IP=$(api $API/sandboxes/$ID | jq -r .ip)
```

**Laptop**, at the `>` prompt:

```
Clone https://github.com/kiyashahidi9/example-express-app into /root/app, install dependencies, start the dev server in the background on port 3000, and add a /hello route that returns hello.
```

- [ ] The chat prints `[run]`, `[read]` and `[write]` steps and ends with `done`.

**VM-2:**

```bash
api $API/sandboxes/$ID/events | jq -r '.events[].type' | sort | uniq -c   # user_message, turn_started, agent_output, turn_finished
state                                                                     # "turn_state":"idle"
curl -s http://$IP:3000/hello; echo                                       # hello
run 'pgrep -f node'                                                       # write down SERVER PID
```

- [ ] Every line prints what its comment says.

---

## Part 4: Auto-pause and wake-up (Track B)

Don't type in the chat. **VM-2**, right after the chat prints `done`:

```bash
watch_state
```

- [ ] Within about 60 s, the state goes `running` → `pausing` → `paused`.

**Laptop:**

```
Write the word "after" to /root/notes.txt and show me the file.
```

- [ ] The chat ends with `done` and shows `after`.

**VM-2:**

```bash
curl -s http://$IP:3000/hello; echo          # hello
run 'pgrep -f node'                          # same SERVER PID
watch_state                                  # back to "paused" within about 60 s
```

- [ ] Every line prints what its comment says.

**Laptop:** `/quit`

- [ ] It prints `sandbox sbx_<id> destroyed`.

---

## Part 5: Recorded agent steps (Track B)

These two parts run the API by hand with a different agent. **VM-1:** Ctrl+C, then:

```bash
cd ~/capstone-project-sandboxes/sbx
sudo systemctl stop sbx-api
sudo install -o root -g sbx-agent -m 0640 examples/agent/replay_sample.py /opt/agents/
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
api -X DELETE $API/sandboxes/$ID; echo
```

- [ ] The types are, in order: `user_message`, `turn_started`, `llm_request`, `llm_response`, `tool_started`, `tool_completed`, `llm_response`, `turn_finished`.
- [ ] `tool_completed` has `"tool_call_id":"call_1"`, `exit_code` 0 and stdout `hi\n`.

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

**VM-1:** Ctrl+C, then go back to the service and its log:

```bash
sudo systemctl start sbx-api && sudo journalctl -u sbx-api -f
```

---

## Part 7: Recovery after a crash, and restore from the bucket (Track C, 7b)

### 7a. A hard crash in the middle of a pause

**VM-2:**

```bash
ID=$(api -X POST $API/sandboxes | jq -r .id)
api -X POST $API/sandboxes/$ID/pause > /dev/null &
sleep 3; sudo systemctl kill -s KILL sbx-api          # a hard crash; systemd starts it again by itself
sleep 5; state                                        # "running" or "paused", never "pausing"
systemctl show sbx-api -p NRestarts                   # NRestarts=1 or more
run 'echo ok'                                         # ok
api -X DELETE $API/sandboxes/$ID; echo                # {"ok":true}
```

- [ ] Every line prints what its comment says.

### 7b. A VM that died, and a leftover the API doesn't know about

**VM-2:**

```bash
ID=$(api -X POST $API/sandboxes | jq -r .id)
sudo pkill -9 -f "$ID/fc.sock"; sleep 1; vm           # no VM
LEFTOVER=$(sudo .venv/bin/python -c "import host_manager; print(host_manager.create_sandbox().id)")
restart_api
state                                                 # "destroyed"
sudo ls /var/sbx                                      # only state.db files (LEFTOVER is gone too)
pgrep -a firecracker; ip netns list                   # nothing
```

- [ ] Every line prints what its comment says.

### 7c. A lost local snapshot is restored from the bucket

**VM-2:**

```bash
ID=$(api -X POST $API/sandboxes | jq -r .id)
put /root/notes.txt 'from the bucket'
api -X POST $API/sandboxes/$ID/pause | jq -c '{state}'          # "paused"
wait_upload                                                     # done
sudo rm -r /var/sbx/$ID/snap /var/sbx/$ID/rootfs.ext4           # lose the local copy
restart_api
state                                                           # still "paused": the bucket can restore it
time get /root/notes.txt                                        # from the bucket (slower: it downloads first)
sudo journalctl -u sbx-api | grep "restored $ID"                # restored ... in N s: write down N
```

- [ ] Every line prints what its comment says. Put N in the download column of the timing table in `readme/Track C README.md`.

### 7d. Lost locally and in the bucket

**VM-2:**

```bash
api -X POST $API/sandboxes/$ID/pause | jq -c '{state}'          # "paused"
wait_upload                                                     # done
sudo rm -r /var/sbx/$ID/snap
gcloud storage rm -r $BUCKET/$ID/                               # no copy anywhere
restart_api
state                                                           # "destroyed"
```

- [ ] Every line prints what its comment says.

---

## Part 8: Swap in your own agent (6a)

**Laptop:** a tiny agent run as a package, which reads a file by a relative path.

```bash
mkdir -p /tmp/testbot/testbot
printf 'import os, sys\nprint("cwd", os.getcwd())\nprint("data", open("data.txt").read().strip())\nprint("msg", sys.stdin.read().strip())\n' > /tmp/testbot/testbot/__main__.py
echo hello-from-data > /tmp/testbot/data.txt
echo 'SECRET=never-copied' > /tmp/testbot/.env
VM_NAME=$VM ZONE=$ZONE sbx/scripts/install.sh --agent /tmp/testbot --cmd "python -m testbot"
```

- [ ] It prints `Installed testbot: AGENT_CMD=/opt/agents/.venv/bin/python -m testbot` and ends with `Done.`

**VM-2:**

```bash
sudo grep -E '^AGENT_(DIR|CMD)=' ../.env      # AGENT_DIR=/opt/agents/testbot and the AGENT_CMD above
sudo ls -a /opt/agents/testbot                # data.txt and testbot, NO .env
ID=$(api -X POST $API/sandboxes | jq -r .id)
api -X POST $API/sandboxes/$ID/messages -H "Content-Type: application/json" -d '{"text":"ping"}' > /dev/null
sleep 3
api $API/sandboxes/$ID/events | jq -r '.events[] | select(.type=="agent_output") | .data.text'
api -X DELETE $API/sandboxes/$ID; echo
```

- [ ] The output is `cwd /opt/agents/testbot`, `data hello-from-data`, `msg ping`.

**Laptop:** switch back to the example agent.

```bash
VM_NAME=$VM ZONE=$ZONE sbx/scripts/install.sh --agent sbx/examples/agent
```

- [ ] It prints `Installed agent: AGENT_CMD=/opt/agents/.venv/bin/python agent.py`.

---

## Part 9: Stop and start the whole VM (6c)

**VM-2:** two running sandboxes, one with a dev server. **Write down both IDs and the IP**: the SSH session ends with the VM.

```bash
ID=$(api -X POST $API/sandboxes | jq -r .id); IP=$(api $API/sandboxes/$ID | jq -r .ip)
put /root/notes.txt 'survives a reboot'
put /root/server.js 'require("http").createServer((q, s) => s.end("ok " + process.pid + "\n")).listen(3000)'
bg 'node /root/server.js'; sleep 2
curl -s http://$IP:3000/                     # ok <PID>: write down PID
SECOND=$(api -X POST $API/sandboxes | jq -r .id)
echo "ID=$ID IP=$IP SECOND=$SECOND"
```

**Laptop:**

```bash
time gcloud compute instances stop $VM --zone=$ZONE    # write down how long it took
gcloud compute instances start $VM --zone=$ZONE
```

Reconnect **VM-1** and **VM-2**, paste the Part 1 helpers, and set `ID`, `IP` and `SECOND` from what you wrote down.

**VM-2:**

```bash
state                                        # "paused"
(ID=$SECOND; state)                          # "paused"
get /root/notes.txt                          # survives a reboot (resumes it, rebuilding its network)
curl -s http://$IP:3000/                     # ok <same PID>
wait_upload                                  # done: uploads cut off by the shutdown start again
```

- [ ] Both sandboxes came back `paused`, and every line prints what its comment says.
- [ ] If one came back `destroyed`, GCP didn't wait long enough for every pause. Note the stop time in the "Shutdown time on GCP" item in `readme/Track C README.md`.

---

## Part 10: Auto-delete forgotten sandboxes (6b)

**VM-2:** pretend the Part 9 sandbox (`$ID`) paused 2 days ago, and turn on a 1-day limit.

```bash
api -X POST $API/sandboxes/$ID/pause | jq -c '{state}'           # "paused"
wait_upload                                                      # done
sudo .venv/bin/python -c "import sqlite3, sys, time; c = sqlite3.connect('/var/sbx/state.db'); c.execute('UPDATE sandboxes SET paused_at = ? WHERE id = ?', (int(time.time()) - 2 * 86400, sys.argv[1])); c.commit()" $ID
sudo sed -i 's/^DELETE_PAUSED_AFTER_DAYS=.*/DELETE_PAUSED_AFTER_DAYS=1/' ../.env
restart_api; sleep 3                                             # the check runs at startup
state                                                            # "destroyed"
gcloud storage ls $BUCKET/$ID/                                   # an error saying it matched no objects
sudo sed -i 's/^DELETE_PAUSED_AFTER_DAYS=.*/DELETE_PAUSED_AFTER_DAYS=/' ../.env
restart_api                                                      # off again
```

- [ ] Every line prints what its comment says.

---

## Part 11: Cleanup

**VM-2:**

```bash
for id in $(api $API/sandboxes | jq -r '.sandboxes[] | select(.state != "destroyed") | .id'); do api -X DELETE $API/sandboxes/$id; done
pgrep -a firecracker; pgrep -u sbx-agent; ip netns list               # nothing
find /sys/fs/cgroup/sandboxes -mindepth 1 -type d                     # nothing
sudo ls /var/sbx                                                      # only state.db files
gcloud storage ls -r $BUCKET/ | grep -c complete                      # 0
```

- [ ] Every line prints what its comment says.

**Laptop:** stop the VM. On a throwaway project, delete everything instead.

```bash
gcloud compute instances stop $VM --zone=$ZONE
# throwaway project only:
# gcloud compute instances delete $VM --zone=$ZONE --quiet
# gcloud storage rm -r gs://$(gcloud config get-value project)-sbx-snapshots
```

---

## Optional: pause and resume timings (about 20 minutes)

Only needed when the pause code changed. It calls the host manager directly, so stop the service first. **VM-2:**

```bash
sudo systemctl stop sbx-api
sudo .venv/bin/python scripts/bench_pause.py          # about 10 minutes
sudo .venv/bin/python scripts/bench_pause.py --cold   # about 10 minutes
sudo systemctl start sbx-api
```

- [ ] Both print a table for 512, 1024 and 2048 MiB with no errors, close to the table in `readme/Track C README.md`.
