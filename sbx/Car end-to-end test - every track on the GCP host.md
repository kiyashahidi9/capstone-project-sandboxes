# Car end-to-end test: Track C on the GCP host

Tests Track C's car work on real Firecracker and a real bucket, from the `track-c-car` branch. About 55 minutes, 20 of them waiting for the install. Best run by a teammate who didn't write `install.sh`.

| Part | Tests |
|---|---|
| 0 | Install into a new project with `install.sh`, then rerun it (6a) |
| 1 | Sandboxes can't reach the VM's bucket credentials |
| 2 | Upload to the bucket after a pause (7a); deleting a sandbox deletes its bucket copies (6b) |
| 3 | Restore from the bucket when the local snapshot is lost (7b) |
| 4 | Swap in your own agent (6a) |
| 5 | Stop and start the whole VM (6c) |
| 6 | Auto-delete forgotten sandboxes (6b) |
| 7 | Cleanup |

**Before you start**
- `gcloud` installed and logged in (`gcloud auth login`).
- A billing account you can use (`gcloud billing accounts list` shows it).
- An OpenAI API key.
- The repo cloned on your laptop. `install.sh` installs the commit you're on, so it must be pushed.
- **On a Mac:** run `setopt interactivecomments` in your laptop terminal first. Otherwise zsh passes the `# …` notes after commands to the command itself, and they fail.

## Terminals

| Name | Open it with | Used for |
|---|---|---|
| **Laptop** | A normal terminal, at the repo root | `install.sh`, `gcloud` |
| **VM-1** | `gcloud compute ssh sbx@$VM --zone=$ZONE`, then `sudo journalctl -u sbx-api -f` | Watching the API's log |
| **VM-2** | `gcloud compute ssh sbx@$VM --zone=$ZONE` | Running commands |

Always log in as **`sbx@`**: that's the account the installer uses, so the repo is at `~/capstone-project-sandboxes`.

---

## Part 0: Install (6a)

**Laptop** (your own computer, not the VM): make a new project and link billing to it.

```bash
PROJECT=sbx-car-test-1                   # project IDs are global: if it's taken, add a number, e.g. sbx-car-test-1-lx
gcloud projects create $PROJECT --name="sbx car test"
gcloud billing accounts list             # copy the ACCOUNT_ID
gcloud billing projects link $PROJECT --billing-account=<ACCOUNT_ID>
gcloud config set project $PROJECT
gcloud billing projects describe $PROJECT    # billingEnabled: true
```

**Laptop:** install from the branch.

```bash
git fetch && git checkout track-c-car && git pull
git status --short                       # nothing: uncommitted changes are NOT installed
export VM=sbx-node-1 ZONE=us-east1-b
VM_NAME=$VM ZONE=$ZONE sbx/scripts/install.sh
```

It asks once for your OpenAI key (typing is hidden), then takes about 15–20 minutes.

- [ ] Steps `==> install 1.` to `==> install 13.` run without `error:` (11 says `none, using the example agent`), and the setup scripts' checklists in between show every line `[x]`.
- [ ] It ends with `Done.` and shows `export SANDBOX_ADMIN_KEY=...`. **Save the key.**

**Laptop:** run it again. Nothing should change.

```bash
VM_NAME=$VM ZONE=$ZONE sbx/scripts/install.sh
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

---

## Part 1: Sandboxes can't reach the bucket credentials

**VM-2:** paste these helpers (again after any reconnect):

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

`col` reads a column straight from the database (`upload_status` isn't in the API). `restart_api` restarts the service and waits until it answers.

**VM-2:**

```bash
ID=$(new)
run 'curl -s -m 3 -H "Metadata-Flavor: Google" http://169.254.169.254/ || echo blocked'   # blocked
```

- [ ] It prints `blocked`: code in a sandbox can't get the VM's bucket credentials.

---

## Part 2: Upload to the bucket, and delete removes it (7a, 6b)

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

## Part 3: Restore from the bucket (7b)

**VM-2:** lose the local snapshot; resume should download it.

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

**VM-2:** lose it locally **and** in the bucket; startup should give up on it.

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

## Part 4: Swap in your own agent (6a)

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

## Part 5: Stop and start the whole VM (6c)

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

Reconnect **VM-1** and **VM-2**, paste the Part 1 helpers, then paste the `ID=… IP=… SECOND=…` line you wrote down.

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

## Part 6: Auto-delete forgotten sandboxes (6b)

**VM-2:** pretend the Part 5 sandbox (`$ID`) paused 2 days ago, and turn on a 1-day limit.

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

## Part 7: Cleanup

**Laptop:** delete the test project, which removes the VM, the bucket and everything else in it, so nothing keeps billing.

```bash
gcloud projects delete sbx-car-test-1    # or the ID you used; asks to confirm
```

- [ ] `gcloud projects list` no longer shows it. Google keeps it restorable for 30 days, but billing stops right away.
