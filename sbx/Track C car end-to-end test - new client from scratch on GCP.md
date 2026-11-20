# Track C car end-to-end test: a new client, from scratch, on GCP

You play a **new client**. You start with nothing but a laptop and a Google account, install the sandbox host into your own GCP project, use it from your laptop the way a client would (SDK, chat app, event stream), then check the host-side guarantees, and finally delete everything.

Run it from the `final-car` branch. Allow about **2 hours**; 20 minutes of that is waiting for the install. It works best if you didn't write `install.sh`.

**What it tests**

| Part | Tests |
|---|---|
| 0 | Laptop tools, and a fresh copy of the repo |
| 1 | A new GCP project with billing |
| 2 | Install with `install.sh`, then rerun it and nothing changes (6a) |
| 3 | Connect from the laptop: tunnel, admin key, `.env` |
| 4 | The Python SDK: create, exec, files, dev server, pause, resume, destroy |
| 5 | The chat app with the example agent; the sandbox pauses itself when idle and wakes up on the next message |
| 6 | The live event stream (`/events/stream`), saved turn by turn |
| 7 | On the host: service, unit tests, and sandboxes can't reach the VM's bucket credentials |
| 8 | Upload to the bucket after a pause (7a); deleting a sandbox deletes its bucket copy (6b) |
| 9 | Restore from the bucket when the local snapshot is lost (7b) |
| 10 | Swap in your own agent (6a) |
| 11 | Stop and start the whole VM (6c) |
| 12 | Auto-delete forgotten sandboxes (6b) |
| 13 | Cleanup: delete the project |

**What it doesn't test:** browser `EventSource` clients, more than one host, other clouds, and replaying an agent run from the event log.

Tick each `- [ ]` as you go. If a line doesn't print what its `#` comment says, stop and note the part, the command and the output.

---

## Part 0: Laptop tools and the repo

You need these on your laptop. Check each one:

```bash
gcloud --version        # Google Cloud CLI. Install: https://cloud.google.com/sdk/docs/install
uv --version            # Python tool. Install: curl -LsSf https://astral.sh/uv/install.sh | sh
git --version
jq --version            # Mac: brew install jq
openssl version         # comes with macOS and Linux
```

- [ ] All five print a version.

You also need:
- A Google account with a **billing account** you're allowed to use.
- An **OpenAI API key** for the example agent. It's stored only on the VM, in a root-only `secrets.env` the agent itself can't read.

**On a Mac**, run this first in every laptop terminal you open, or zsh passes the `# …` notes after commands to the commands, and they fail:

```bash
setopt interactivecomments
```

**Laptop:** log in to Google Cloud.

```bash
gcloud auth login                         # opens a browser; pick your account
gcloud billing accounts list              # shows at least one account with OPEN = True
```

**Laptop:** get a fresh copy of the repo, as a client would. Don't reuse your working copy.

```bash
mkdir -p ~/sbx-client-test && cd ~/sbx-client-test
git clone https://github.com/kiyashahidi9/capstone-project-sandboxes.git
cd capstone-project-sandboxes
git checkout final-car
git status --short                        # prints nothing
```

- [ ] `git checkout final-car` works. If it says the branch doesn't exist, ask whoever owns it to push it.

From here on, **Laptop** means a terminal at `~/sbx-client-test/capstone-project-sandboxes`.

---

## Part 1: A new GCP project

**Laptop:**

```bash
PROJECT=sbx-client-test-1                 # IDs are global: if it's taken, add something, e.g. sbx-client-test-1-lx
gcloud projects create $PROJECT --name="sbx client test"
gcloud billing accounts list              # copy the ACCOUNT_ID
gcloud billing projects link $PROJECT --billing-account=<ACCOUNT_ID>
gcloud config set project $PROJECT
gcloud billing projects describe $PROJECT # billingEnabled: true
```

- [ ] The last line shows `billingEnabled: true`.

---

## Part 2: Install (6a)

**Laptop:**

```bash
export VM=sbx-node-1 ZONE=us-east1-b
VM_NAME=$VM ZONE=$ZONE sbx/scripts/install.sh
```

It asks once for the example agent's OpenAI key (typing is hidden) at step 13, then finishes. It takes about 15–20 minutes.

Later parts reuse `$PROJECT`, `$VM` and `$ZONE`. In a new laptop terminal, set them again first: `PROJECT=<your project id> VM=sbx-node-1 ZONE=us-east1-b`.

- [ ] Steps `==> install 1.` to `==> install 14.` run without `error:`. Step 11 says `none, using the example agent`. The setup scripts' checklists in between show every line `[x]`.
- [ ] It ends with `Done.` and prints `export SANDBOX_ADMIN_KEY=...`. **Copy that line somewhere safe.** It's shown only this once.

If it stops with an `error:`, the message says what to do (for example, an organization policy blocking nested virtualization). Note it and stop.

**Laptop:** run it again. Nothing should change.

```bash
VM_NAME=$VM ZONE=$ZONE sbx/scripts/install.sh
```

- [ ] The steps that create something say `already exists`, including `.env (keeping its admin key)` and `the example agent's secrets.env`. It doesn't ask for the OpenAI key.
- [ ] It says `nothing changed, so the running API was left alone`, and shows **no** new key.

---

## Part 3: Connect from the laptop

The API listens only inside the VM. Clients reach it through an SSH tunnel.

**Laptop, a new terminal (the "Tunnel" terminal).** Leave it open for Parts 3–6. It prints nothing while it works.

```bash
gcloud compute ssh sbx@sbx-node-1 --zone=us-east1-b -- -N -L 8000:localhost:8000
```

**Laptop:** give your laptop the admin key. Paste the `export` line you saved in Part 2, then write the repo-root `.env` that the SDK reads.

```bash
export SANDBOX_ADMIN_KEY=<the key from Part 2>
printf 'SANDBOX_ADMIN_KEY=%s\nSANDBOX_API=http://localhost:8000\n' "$SANDBOX_ADMIN_KEY" > .env
cat .env                                  # two lines: the key, and SANDBOX_API
```

The laptop has no OpenAI key. The agent runs on the VM, and even it never holds the key: its model calls go through the API's key proxy, which adds the key from `secrets.env`.

**Laptop:** check the connection.

```bash
api() { curl -s -H "Authorization: Bearer $SANDBOX_ADMIN_KEY" "$@"; }
api localhost:8000/sandboxes; echo                         # {"sandboxes":[]}
curl -s -o /dev/null -w '%{http_code}\n' localhost:8000/sandboxes   # 401: no key, no access
```

- [ ] The first prints `{"sandboxes":[]}` and the second prints `401`.

**Laptop:** install the Python dependencies.

```bash
cd sbx && uv sync && cd ..
```

- [ ] It ends without errors.

---

## Part 4: The Python SDK

This is what a client's own code does. **Laptop:**

```bash
cd sbx
uv run python - <<'EOF'
import time
from sdk.sandbox import Sandbox, SandboxError    # importing it loads ../.env
import os

s = Sandbox.create(token=os.environ["SANDBOX_ADMIN_KEY"])
print("1 created", s.id)

r = s.exec("echo hi from $(hostname); exit 3")
print("2 exec", repr(r["stdout"].strip()), "exit", r["exit_code"])

s.write_file("/root/notes.txt", "hello from the client")
print("3 file", s.read_file("/root/notes.txt"))

s.write_file("/root/server.js",
    'require("http").createServer((q, s) => s.end("ok " + process.pid)).listen(3000)')
s.exec("node /root/server.js", background=True)
time.sleep(2)
before = s.exec("curl -s localhost:3000")["stdout"]
print("4 server", before)

t = time.time(); s.pause()
print(f"5 paused in {time.time() - t:.0f}s")

t = time.time(); text = s.read_file("/root/notes.txt")
print(f"6 file after pause {text!r}, woke up in {time.time() - t:.1f}s")

after = s.exec("curl -s localhost:3000")["stdout"]
print("7 same server process:", after == before)

s.destroy()
try:
    s.exec("true")
except SandboxError as e:
    print("8 after destroy:", e.status)
EOF
cd ..
```

Expected, line by line:

| Line | Shows |
|---|---|
| 1 | `created sbx…` |
| 2 | `'hi from …'` and `exit 3`: a failing command doesn't raise, it returns its exit code |
| 3 | `hello from the client` |
| 4 | `ok <PID>` |
| 5 | about 10–20 s |
| 6 | `'hello from the client'`, woke up in a few seconds: reading a file resumes a paused sandbox |
| 7 | `True`: the dev server kept running through the pause |
| 8 | `409`: a destroyed sandbox can't be used |

- [ ] All eight lines match. Write down the times from lines 5 and 6.

---

## Part 5: Chat with the example agent

The chat app sends your messages to the agent on the VM and prints what comes back over the event stream.

**Laptop:**

```bash
cd sbx
uv run python -m client.client
```

- [ ] It prints `sandbox sbx… ready` and a `>` prompt.

Type these one at a time. Wait for `done` after each.

1. `create /root/hello.py that prints hello world, and run it`
   - [ ] The agent's steps print, including `hello world`, then `done`.
2. **Wait 60 seconds without typing.** About 30 seconds after a turn ends, a quiet sandbox pauses itself. To check it from another laptop terminal (paste the `api` helper and the `export SANDBOX_ADMIN_KEY` line there first):
   ```bash
   api localhost:8000/sandboxes | jq -c '.sandboxes[] | {id, state}'   # "state":"paused"
   ```
   - [ ] It shows `"paused"`.
3. `what does /root/hello.py print? run it to check`
   - [ ] Before the answer, the app prints `[paused]` then `[resumed]`: the message woke the sandbox up.
   - [ ] The agent finds the file it made earlier and prints `hello world`, then `done`. Nothing was lost in the pause.
4. `/quit`
   - [ ] It prints `sandbox sbx… destroyed`.

```bash
cd ..
```

---

## Part 6: The live event stream

A client's orchestrator can subscribe to one sandbox's events instead of polling. This script subscribes, gives the agent three short tasks, and saves what arrived for each turn.

**Laptop:**

```bash
cd sbx
uv run python scripts/test_check_sandbox_subscription.py
```

It prints each event as a JSON line as it arrives, and `saved stream-log/<id>/turn-N.json` after each turn.

```bash
ls stream-log/*/                                              # turn-1.json turn-2.json turn-3.json
for f in stream-log/*/turn-*.json; do echo "$f: $(jq -r '.[-1].type' $f)"; done   # each ends in turn_finished
jq -r '.[] | select(.type=="agent_output") | .data.text' stream-log/*/turn-2.json   # includes 1
rm -r stream-log                                              # don't commit it
cd ..
```

- [ ] There are three files and each one's last event is `turn_finished`.
- [ ] Turn 2's output includes `1`, the contents of the file turn 1 made.
- [ ] The script ended with `sandbox sbx… destroyed`.

You can close the Tunnel terminal now.

---

## Part 7: On the host

Now check the host from the inside. Open two more terminals:

| Name | Open it with | Used for |
|---|---|---|
| **VM-1** | `gcloud compute ssh sbx@sbx-node-1 --zone=us-east1-b`, then `sudo journalctl -u sbx-api -f` | Watching the API's log |
| **VM-2** | `gcloud compute ssh sbx@sbx-node-1 --zone=us-east1-b` | Running commands |

Always log in as **`sbx@`**: that's the account the installer uses, so the repo is at `~/capstone-project-sandboxes`.

**VM-2:** check the service, then run the unit tests on the host.

```bash
systemctl is-active sbx-api; systemctl is-enabled sbx-api   # active, enabled
cd ~/capstone-project-sandboxes/sbx
sudo systemctl stop sbx-api
sudo .venv/bin/pytest -q                  # sudo: .env is root only, and the real Firecracker tests need root
sudo systemctl start sbx-api
```

- [ ] pytest shows no `failed` and no `skipped`.

**VM-2:** paste these helpers. Paste them again after any reconnect.

```bash
cd ~/capstone-project-sandboxes/sbx
KEY=$(sudo grep '^SANDBOX_ADMIN_KEY=' ../.env | cut -d= -f2-)
BUCKET=$(sudo grep '^SNAPSHOT_BUCKET=' ../.env | cut -d= -f2-)
API=localhost:8000
api()   { curl -s -H "Authorization: Bearer $KEY" "$@"; }
new()   { api -X POST $API/sandboxes | jq -r .id; }
state() { api $API/sandboxes/$ID | jq -c '{state, paused_at}'; }
pause() { api -X POST $API/sandboxes/$ID/pause | jq -c '{state}'; }
run()   { api -X POST $API/sandboxes/$ID/exec -H "Content-Type: application/json" \
            -d "$(jq -n --arg c "$1" '{cmd: $c}')" | jq -r '.stdout // .detail'; }
bg()    { api -X POST $API/sandboxes/$ID/exec -H "Content-Type: application/json" \
            -d "$(jq -n --arg c "$1" '{cmd: $c, background: true}')" | jq -c .; }
put()   { api -X PUT "$API/sandboxes/$ID/files?path=$1" --data-binary "$2" | jq -c .; }
get()   { api "$API/sandboxes/$ID/files?path=$1"; echo; }
col()   { sudo .venv/bin/python -c "import sqlite3, sys; print(sqlite3.connect('/var/sbx/state.db').execute(f'SELECT {sys.argv[2]} FROM sandboxes WHERE id = ?', (sys.argv[1],)).fetchone()[0])" $ID $1; }
wait_upload() { for i in $(seq 60); do [ "$(col upload_status)" = done ] && break; sleep 5; done; col upload_status; }
restart_api() { sudo systemctl restart sbx-api; until curl -s -o /dev/null $API/sandboxes; do sleep 1; done; }
```

`col` reads a column straight from the database (`upload_status` isn't in the API). `restart_api` restarts the service and waits until it answers. None of the sandboxes below get agent messages, so none of them pause by themselves.

**VM-2:** a sandbox must not be able to reach the VM's Google credentials.

```bash
ID=$(new)
run 'curl -s -m 3 -H "Metadata-Flavor: Google" http://169.254.169.254/ || echo blocked'   # blocked
```

- [ ] It prints `blocked`.

---

## Part 8: Upload to the bucket, and delete removes it (7a, 6b)

**VM-2** (same `$ID`):

```bash
pause                                        # {"state":"paused"}
wait_upload                                  # done (can take a few minutes: ~5 GiB)
gcloud storage ls -r $BUCKET/$ID/            # one folder with complete, mem, rootfs.ext4, vmstate
api -X DELETE $API/sandboxes/$ID; echo       # {"ok":true}
gcloud storage ls $BUCKET/$ID/               # an error saying it matched no objects
```

- [ ] Every line prints what its comment says.

---

## Part 9: Restore from the bucket (7b)

**VM-2:** lose the local snapshot. Resuming should download it from the bucket.

```bash
ID=$(new)
put /root/notes.txt 'from the bucket'
pause                                                           # {"state":"paused"}
wait_upload                                                     # done
sudo rm -r /var/sbx/$ID/snap /var/sbx/$ID/rootfs.ext4           # lose the local copy
restart_api
state                                                           # still "paused": the bucket can restore it
time get /root/notes.txt                                        # from the bucket (slower: it downloads first)
sudo journalctl -u sbx-api | grep "restored $ID"                # restored ... in N s: write down N
```

- [ ] Every line prints what its comment says. Put N in the download column of the timing table in `readme/Track C README.md`.

**VM-2:** lose it locally **and** in the bucket. Startup should give up on it.

```bash
pause                                                           # {"state":"paused"}
wait_upload                                                     # done
sudo rm -r /var/sbx/$ID/snap
gcloud storage rm -r $BUCKET/$ID/                               # no copy anywhere
restart_api
state                                                           # "destroyed"
```

- [ ] Every line prints what its comment says.

---

## Part 10: Swap in your own agent (6a)

A client brings their own agent. **Laptop:** a tiny agent run as a package, which reads a file by a relative path and has a `.env` that must never be copied to the VM.

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
ID=$(new)
api -X POST $API/sandboxes/$ID/messages -H "Content-Type: application/json" -d '{"text":"ping"}' > /dev/null
sleep 3
api $API/sandboxes/$ID/events | jq -r '.events[] | select(.type=="agent_output") | .data.text'
api -X DELETE $API/sandboxes/$ID; echo        # {"ok":true}
```

- [ ] The output is `cwd /opt/agents/testbot`, `data hello-from-data`, `msg ping`.

**Laptop:** switch back to the example agent.

```bash
VM_NAME=$VM ZONE=$ZONE sbx/scripts/install.sh --agent sbx/examples/agent
```

- [ ] It prints `Installed agent: AGENT_CMD=/opt/agents/.venv/bin/python agent.py`.

---

## Part 11: Stop and start the whole VM (6c)

**VM-2:** two running sandboxes, one with a dev server.

```bash
ID=$(new); IP=$(api $API/sandboxes/$ID | jq -r .ip)
put /root/notes.txt 'survives a reboot'
put /root/server.js 'require("http").createServer((q, s) => s.end("ok " + process.pid + "\n")).listen(3000)'
bg 'node /root/server.js'; sleep 2
curl -s http://$IP:3000/                     # ok <PID>
SECOND=$(new)
echo "ID=$ID IP=$IP SECOND=$SECOND"          # write this line down: the SSH session ends with the VM
```

**Laptop:**

```bash
time gcloud compute instances stop $VM --zone=$ZONE    # write down how long it took
gcloud compute instances start $VM --zone=$ZONE
```

Reconnect **VM-1** and **VM-2**, paste the Part 7 helpers into VM-2, then paste the `ID=… IP=… SECOND=…` line you wrote down.

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

## Part 12: Auto-delete forgotten sandboxes (6b)

**VM-2:** pretend the Part 11 sandbox (`$ID`) paused 2 days ago, and turn on a 1-day limit.

```bash
pause                                                            # {"state":"paused"}
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

## Part 13: Cleanup

**Laptop:** delete the test project. That removes the VM, the bucket and everything else in it, so nothing keeps billing.

```bash
gcloud projects delete $PROJECT          # asks to confirm
gcloud config unset project
rm -rf ~/sbx-client-test /tmp/testbot
```

- [ ] `gcloud projects list` no longer shows it. Google keeps it restorable for 30 days, but billing stops right away.

---

## Report back

Send the owner of this branch:
- Every box you couldn't tick, with the part, the command and its output.
- The times from Part 4 (pause, wake-up), Part 9 (N, the bucket restore) and Part 11 (VM stop).
