# Persistent Sandboxes

Persistent Firecracker sandboxes for long-running coding agents. Each sandbox is a Firecracker microVM on a Linux host with KVM. An API server manages them, and a Python SDK and example agents use that API.

```text
Client / AI Agent (example_agent, client)
       |
       | Python SDK (sdk/)
       v
 FastAPI Server (api/main.py)
       |
       | Node Agent functions (node_agent.py)
       v
    Node Agent
       |
       | Firecracker API
       v
 Firecracker MicroVM
       |
       v
    Guest Agent (guest_agent/)
```

## Contents

- [Repository layout](#repository-layout)
- [Setup](#setup)
  - [Host on Google Cloud](#host-on-google-cloud)
  - [Connecting from your computer](#connecting-from-your-computer)
  - [Using the client agent](#using-the-client-agent)
  - [Running the tests](#running-the-tests)
- [Step 1F: Node agent](#step-1f-node-agent)
- [Step 1G: API server](#step-1g-api-server)
- [Steps 1H/I: SDK and example agents](#steps-1hi-sdk-and-example-agents)
- [To revisit](#to-revisit)
- [Stretch goals](#stretch-goals)

---

## Repository layout

All program code lives in `sbx/`. The `.env` file stays at the repo root.

```
sbx/
  api/             HTTP API server (FastAPI) and its SQLite tables       (1G)
  db/              database schema                                       (1G)
  node_agent.py    controls Firecracker sandboxes from Python            (1F)
  scripts/         host setup, networking and guest agent install scripts
  guest_agent/     HTTP server that runs inside the "VM"
  sdk/             Python SDK + smoke test                               (1H/I)
  client/          interactive chat agent (one sandbox across tasks)     (1H/I)
  example_agent/   one-shot OpenAI coding agent                          (1H/I)
  tests/           pytest suite
```

---

## Setup

### Host on Google Cloud

Sandboxes are Firecracker microVMs, which need a Linux host with KVM. These steps create the host on Google Cloud and set it up.

#### 1. Install the Google Cloud CLI (on your computer)

Install it from https://cloud.google.com/sdk/docs/install, then log in and choose your project:

```bash
gcloud init
```

#### 2. Create the host VM

A normal cloud VM doesn't expose KVM, so turn on nested virtualization. Use an Intel machine type such as N2, where it's best supported.

```bash
gcloud compute instances create sbx-node-1 \
  --zone=us-east1-b \
  --machine-type=n2-standard-8 \
  --image-family=ubuntu-2404-lts-amd64 \
  --image-project=ubuntu-os-cloud \
  --boot-disk-size=200GB \
  --enable-nested-virtualization
```

#### 3. Connect to it

```bash
gcloud compute ssh sbx-node-1 --zone=us-east1-b
```

For VS Code Remote-SSH, run `gcloud compute config-ssh` (again after each restart; the IP changes).

#### 4. Set up the host (on the VM)

```bash
git clone https://github.com/kiyashahidi9/capstone-project-sandboxes.git
cd capstone-project-sandboxes/sbx
chmod +x scripts/*
sudo scripts/setup_host.sh
```

This takes a few minutes. It:

1. installs host packages and checks KVM (`kvm-ok`) and cgroups v2,
2. installs Firecracker (pinned version, so everyone matches),
3. downloads the kernel and builds the base image in `~/fc`: Ubuntu 24.04 with Python, git and Node,
4. bakes the guest agent into the image (`scripts/install_guest_agent.sh`),
5. creates the Python environment `.venv`,

and ends with a checklist. Every line should show `[x]`. It's safe to rerun. To rebuild the base image from scratch, run `sudo scripts/setup_host.sh --rebuild-rootfs`.

#### 5. Start the API server (on the VM)

From `sbx/`:

```bash
sudo .venv/bin/uvicorn api.main:app --host 0.0.0.0 --port 8000
```

Leave it running. Check it from a second terminal on the VM with `curl http://localhost:8000/sandboxes`, which should return `[]`. For more detail, see [Start the API server](#start-the-api-server).

#### Stopping and starting the VM

```bash
gcloud compute instances stop sbx-node-1 --zone=us-east1-b
gcloud compute instances start sbx-node-1 --zone=us-east1-b
```

Stopping ends every running sandbox. After starting, start the API server again (see [Start the API server](#start-the-api-server)).

### Connecting from your computer

The SDK and agents talk to the API server running on the GCP VM (see [Start the API server](#start-the-api-server)). Reach it through an SSH tunnel.

```bash
# install uv if you don't have it: https://docs.astral.sh/uv/getting-started/installation/

# set up env vars (at the repo root), then fill in OPENAI_API_KEY in .env
# SANDBOX_API defaults to http://localhost:8000, the local end of the tunnel
cp .env.example .env

# run every command below from sbx/
cd sbx

# create .venv and install the locked dependencies
uv sync

# terminal 2: forward localhost:8000 to the API on the VM (unless VS code remote does it already)
gcloud compute ssh sbx-node-1 --zone=us-east1-b -- -N -L 8000:localhost:8000

# check the API is reachable
curl http://localhost:8000/sandboxes
```

### Using the client agent

The client agent is an interactive chat. It opens one sandbox and keeps it for every task in the session. It needs the API server running on the VM, the SSH tunnel open, and `OPENAI_API_KEY` set in `.env` (see [Connecting from your computer](#connecting-from-your-computer)).

Start it from `sbx/`:

```bash
uv run python -m client.client
```

When it prints `sandbox <id> ready`, type a task at the `>` prompt. The agent runs commands and edits files in the sandbox, starting from `/root`, and then prints its answer and `done`. Later tasks run in the same sandbox, so you can build on earlier work:

```
> clone https://github.com/kiyashahidi9/example-express-app and start the dev server
> curl the health check
> add a /time route that returns the current time, then curl it
```

Type `/quit` to exit. The sandbox is destroyed when you exit, including with Ctrl+C. For the one-shot agent and more examples, see [Running the agents](#running-the-agents).

### Running the tests

The API and database tests use a fake node agent, so they do not boot a virtual machine and they do not need root.

From `sbx/`, with [uv](https://docs.astral.sh/uv/) installed and Python 3.11 or newer:

```bash
cd sbx
uv sync
uv run pytest tests/test_api.py tests/test_db.py -v
```

`uv sync` installs the project, including pytest.

Run one file:

```bash
uv run pytest tests/test_api.py -v
uv run pytest tests/test_db.py -v
```

| File | What it covers |
|------|----------------|
| `tests/test_api.py` | Create, list, get, delete, exec, and file read/write. The boot, command, and file calls are faked. |
| `tests/test_db.py` | The SQLite tables: insert, state changes, lookup, and list order. Uses a temporary database file. |

`uv run pytest` also runs `tests/test_node_agent_helpers.py`. That file checks slot allocation and does not need a virtual machine. `tests/test_node_agent.py` boots real Firecracker virtual machines and skips itself on a machine without root and `/dev/kvm`.

---

## Step 1F: Node agent

Short record of design decisions in `node_agent.py` and why we made them.

### 1. Two identifiers: `sandbox_id` and `n`

They do different jobs and are **not** derived from each other.

| | `sandbox_id` (str) | `n` (int) |
|---|---|---|
| Example | `sbx_3f9c1a7e…` (`uuid4`) | `3` |
| Job | The sandbox's name | Its network slot |
| Reused? | Never | Yes, after the sandbox is destroyed |
| Used for | Folders, cgroup, API URLs, DB key, (later) events and snapshots | `10.201.0.3`, `10.200.3.x`, namespace `sbx3`, `veth3h`, network script args |

**Why not one value?** The network needs a small number: IPs only fit 0–255 per part, and Linux interface names max out at 15 characters (a UUID is 32). With ~250 slots, numbers must be reused. When the ID was `sbx<n>`, reusing the number reused the name, so anything still holding an old ID (an agent, the DB, event logs, snapshots) would reach the *new* sandbox.

**The link:** the `slots` table in `/var/sbx/state.db` (SQLite) is the only record of which sandbox holds which slot. `find_slot()` reads it. See section 3.

**Rule:** public functions take only `sandbox_id`. Code that needs the slot calls `require_slot()`, which also rejects bad IDs (e.g. `"../etc"`) before they're used in paths we delete, and raises `LookupError` for unknown sandboxes.

### 2. Where commands run

| Where | How | Example |
|---|---|---|
| Host | `subprocess.run` | `cp`, `netns_create.sh 3` |
| Host, inside namespace `sbx3` (slot 3) | `ip netns exec sbx3 ...` | Firecracker |
| Inside the VM | HTTP to the guest agent | `exec("sbx_3f9c…", "npm install")` |

- Host commands inherit Python's user (root), environment and working folder. Scripts are run by full path, so the working folder doesn't matter.
- The VM is a separate computer, so the host can't run programs in it directly. That's why the guest agent exists.

### 3. Picking a slot: `claim_free_slot()`

> **Open:** the 250 cap comes from the IP (`10.201.0.<n>`), but RAM is the real limit: ~20 sandboxes on the current machine at 1 GiB per VM. Lower `MAX_SANDBOX_NUMBER` to match so a full host gives a clean error.

Gives each new sandbox the **lowest free slot** `n` (1, 2, 3, …). Slots are stored in SQLite:

```sql
CREATE TABLE slots (
  n           INTEGER PRIMARY KEY CHECK (n BETWEEN 1 AND 250),  -- one row per taken slot
  sandbox_id  TEXT NOT NULL UNIQUE                              -- a sandbox holds at most one slot
);
```

| Function | SQL | Notes |
|---|---|---|
| `claim_free_slot(id)` | `INSERT INTO slots VALUES (n, id)` for n = 1, 2, 3, … | First insert that succeeds wins; `IntegrityError` means taken, try the next |
| `find_slot(id)` | `SELECT n FROM slots WHERE sandbox_id = ?` | Indexed (the `UNIQUE` column), no scanning |
| `release_slot(n, id)` | `DELETE FROM slots WHERE n = ? AND sandbox_id = ?` | The owner check is part of the query |

- **Claim = one `INSERT`.** The `PRIMARY KEY` on `n` rejects a second insert for the same slot, so the check and the claim happen in one step and two requests at once can't get the same slot (`test_concurrent_claims_never_share_a_slot` runs 20 claims at the same moment).
- **Freed slots are reused**, so we never run out of IPs (`10.201.0.<n>` can't go above 254). The ID is not reused: the next sandbox in slot 3 gets a new UUID.
- **Free the slot last.** `destroy_sandbox` releases the slot only after the network is deleted, so the next sandbox in that slot can't collide with a leftover `veth3h`. If the delete fails, the slot stays taken.
- **Only the owner can free it.** `release_slot(n, id)` deletes the row only if `id` still holds slot `n`.

#### How we use SQLite safely

| Rule | Why |
|---|---|
| **Each statement is atomic on its own**, so single statements need no `BEGIN … COMMIT` | SQLite wraps every statement in a transaction. Our three functions are one statement each |
| Use `BEGIN IMMEDIATE … COMMIT` when **a read decides a write**, or **two writes must stay consistent** | Otherwise two callers can both read "slot 3 is free" (a check-then-act race), or a crash between two writes leaves them disagreeing. `IMMEDIATE` takes the write lock up front, so a second caller waits instead of failing |
| Open with `isolation_level=None` | Turns off Python's hidden transactions, so each statement commits on its own and any `BEGIN` is ours |
| `timeout=5` and `PRAGMA journal_mode=WAL` | A second writer waits up to 5 s instead of failing with "database is locked"; readers aren't blocked while someone writes |
| **A new connection per call** | The API server calls us from several threads, and a SQLite connection shouldn't be shared between threads |

### 4. cgroups: one meter per sandbox

A **cgroup** (control group) is a Linux group of processes whose CPU and memory use can be **measured** (and later **limited**). You work with it through special folders that Linux fills in live:

| Action | How |
|---|---|
| Create a group | `mkdir /sys/fs/cgroup/sandboxes/<sandbox_id>` (parent folder = `CGROUP_ROOT`) |
| Add a process | Write its PID to `.../<sandbox_id>/cgroup.procs` |
| Read CPU use | `.../<sandbox_id>/cpu.stat` |

**Why:** each sandbox's Firecracker process gets its own group, so Milestone 2's sampler can see how busy each VM is **from the host**, with no code inside the VM.

**Order matters:** a cgroup can only be removed once its process is gone, so `destroy_sandbox` kills Firecracker first.

### 5. One sandbox = one Firecracker process

```
Host (GCP machine)
├── python (node agent / API server)     PID 1000
├── firecracker  ← sandbox sbx_8f3a… (slot 1)   PID 2041
├── firecracker  ← sandbox sbx_b21c… (slot 2)   PID 2077
└── firecracker  ← sandbox sbx_c90d… (slot 3)   PID 2103
```

That one process **is** the whole VM. Everything inside it (guest agent, `npm`, dev servers) is hidden inside that PID.

- **Kill the PID → the whole sandbox stops.**
- **One cgroup per sandbox** measures everything in the VM.
- **The PID is saved** (`firecracker.pid`), so a restarted node agent can still find and kill it.
- **The PID changes on resume** (Milestone 3): pause kills the process, resume starts a new one. The sandbox ID and slot stay the same (a paused sandbox keeps its slot, since its network must still be there on wake-up).

### 6. What the API server (1G) gets from here

| 1G needs | Node agent gives it |
|---|---|
| Save the DB row as `starting` *before* booting | `new_sandbox_id()`, then `create_sandbox(sandbox_id)` |
| 404 for an unknown sandbox | `exec` / `read_file` / `write_file` raise `LookupError` (unknown) or `ValueError` (malformed ID) |
| On startup: is each DB sandbox still alive? | `is_sandbox_running(id)` |
| On startup: anything on the host the DB doesn't know? | `list_host_sandboxes()` (then `destroy_sandbox` each orphan) |
| Restart the server without killing VMs | Firecracker starts in its own session, so Ctrl+C on uvicorn doesn't reach it |

**Still 1G's job:** its own `sandboxes` table (state, pid, timestamps) in the **same** `state.db`. It shouldn't store `n` again: `JOIN slots ON slots.sandbox_id = sandboxes.id` or `find_slot(id)` gives the slot, so there is only one copy. Also one lock per sandbox, so `exec` and `destroy` for the same ID can't overlap.

### 1F tradeoffs

#### Two identifiers (section 1)

One extra lookup per call (an indexed SQLite query), and two identifiers to keep straight. Long IDs are harder to read in logs; print the first 8 characters if needed.

#### Calling bash scripts from Python (section 2)

We call our tested 1D bash scripts instead of rewriting networking in Python (e.g. `pyroute2`). That means less new code, but errors come back as script output instead of Python exceptions, so `run_script` includes that output in the error.

#### Always running `netns_delete.sh` (section 6)

`delete_network()` always runs `netns_delete.sh` instead of first checking the namespace exists. It's one extra script call per destroy, but it also cleans up half-built networks (e.g. a host route left without its namespace), and the script fails if anything for the slot remains, so the slot is never handed out dirty.

#### Decision 1: UUID IDs instead of `sbx<n>`

| | `sbx<n>` (before) | `sbx_<uuid4>` + separate slot `n` (now) |
|---|---|---|
| An old ID can reach a new sandbox | **Yes**: slot numbers are reused, so names were too | No: IDs are never reused |
| Works as a key for the DB (1G), events (M5), snapshots (M6) | No: collisions after reuse | Yes |
| Finding the slot | Parse it out of the name | One lookup |
| Readability | `sbx3` | `sbx_3f9c1a7e…` (print the first 8 characters) |

**Why `uuid4`:** Python 3.11 has no `uuid7`, and at our scale ID ordering doesn't matter. Short random IDs (e.g. 4 hex characters) collide after a few hundred sandboxes; auto-increment integers are guessable and still need a separate slot.

#### Decision 2: where to record which sandbox holds which slot

We considered three options. Each claims a slot atomically, so all three are safe when two sandboxes are created at once:

| | Folder `/var/sbx/sbx<n>` (`mkdir`) | Symlink `slots/<n> -> <id>` | **SQLite `slots` table (chosen)** |
|---|---|---|---|
| Atomic claim | ✅ `mkdir` fails if it exists | ✅ `symlink` fails if it exists | ✅ `PRIMARY KEY` rejects duplicates |
| Records the owner | ❌ (only worked while ID = `sbx<n>`) | ✅ in the link's text | ✅ `sandbox_id` column |
| Owner check on release | — | Read, compare, then delete | Built into the `DELETE` |
| Find a sandbox's slot | — | Scan up to 250 links | One indexed query |
| One record shared with 1G | ❌ | ❌ The API compares two places on startup | ✅ 1G's `sandboxes` table lives in the same file |
| Setup | None | None | A DB file (`sqlite3` ships with Python) |
| Thread care | None | None | A connection per call, wait timeout, WAL |
| Inspect it | `ls /var/sbx` | `ls -l /var/sbx/slots` | `sqlite3 /var/sbx/state.db "SELECT * FROM slots"` |

**Why SQLite:** 1G needs a database anyway, so keeping slots there gives one source of truth instead of two records compared on startup, plus indexed lookups and a built-in owner check. The cost is a little setup and thread care, listed above.

**What we did *not* gain:** neither a symlink nor a DB row can see the kernel. Either can say a slot is free while `veth3h` still exists (e.g. after a crash or a manual delete). What keeps slots safe is the same in every option: **free the slot last**, and `netns_delete.sh` **fails if anything is left**, so a dirty slot is never handed out.

**Considered, not chosen: the API picks the slot.** 1G could choose `n` and pass it to `create_sandbox(id, n)`. That also gives one source of truth, but the node agent couldn't run on its own (the 1F tests would need the API). Keeping the claim in the node agent, in a database file 1G shares, gives us both.

---

## Step 1G: API server

### Overview

Build a REST API using **FastAPI** that exposes the Node Agent's sandbox management functionality to external clients.

The API server acts as the entry point for clients and AI agents to interact with Firecracker MicroVM sandboxes.

**Responsibilities:**
- Expose endpoints for sandbox lifecycle management.
- Execute commands inside running sandboxes.
- Read and write files inside sandboxes.
- Persist sandbox metadata using SQLite.
- Recover sandbox state after an API server restart.
- Handle invalid sandbox IDs and states.

**Execution environment:** The API server runs as root on the Linux host to access the Node Agent's privileged operations.

### REST API endpoints

**File:** `api/main.py`

| Method | Endpoint | Node Agent Function | Response |
|--------|----------|---------------------|----------|
| POST | `/sandboxes` | `create_sandbox()` | Sandbox ID and state |
| GET | `/sandboxes` | — | List of all sandboxes |
| GET | `/sandboxes/{id}` | — | Sandbox metadata |
| POST | `/sandboxes/{id}/exec` | `exec()` | Command execution result |
| GET | `/sandboxes/{id}/files?path=` | `read_file()` | File contents |
| PUT | `/sandboxes/{id}/files?path=` | `write_file()` | Success confirmation |
| DELETE | `/sandboxes/{id}` | `destroy_sandbox()` | Success confirmation |

#### Example responses

**Create a sandbox**

```http
POST /sandboxes
```

```json
{
  "id": "sbx1",
  "state": "running"
}
```

**List all sandboxes**

```http
GET /sandboxes
```

```json
[
  {
    "id": "sbx1",
    "state": "running"
  },
  {
    "id": "sbx2",
    "state": "destroyed"
  }
]
```

**Get sandbox details**

```http
GET /sandboxes/sbx1
```

```json
{
  "id": "sbx1",
  "state": "running",
  "created_at": 1794470400
}
```

**Execute a command**

```http
POST /sandboxes/sbx1/exec
Content-Type: application/json
```

Request body:

```json
{
  "cmd": "uname -a"
}
```

Return the Guest Agent's command execution result.

**Read a file**

```http
GET /sandboxes/sbx1/files?path=/tmp/hello.txt
```

Return the requested file contents.

**Write a file**

```http
PUT /sandboxes/sbx1/files?path=/tmp/hello.txt
```

Pass the request body to `write_file()`.

Response:

```json
{
  "ok": true
}
```

**Destroy a sandbox**

```http
DELETE /sandboxes/sbx1
```

```json
{
  "ok": true
}
```

### Error handling

| Status Code | Condition |
|-------------|-----------|
| 404 Not Found | Sandbox ID does not exist |
| 409 Conflict | Operation requires a running sandbox, but it is not running |

#### Example: unknown sandbox

```http
GET /sandboxes/unknown
```

```json
{
  "detail": "Sandbox not found"
}
```

**HTTP status:** `404`

#### Example: sandbox not running

```http
POST /sandboxes/sbx1/exec
```

```json
{
  "detail": "Sandbox is not running"
}
```

**HTTP status:** `409`

### SQLite state persistence

**Database:** `state.db`

Store sandbox metadata in SQLite so that the API server can recover existing sandboxes after restarting.

#### Database schema

```sql
CREATE TABLE sandboxes (
    id          TEXT PRIMARY KEY,
    n           INTEGER NOT NULL,
    ip          TEXT NOT NULL,
    pid         INTEGER,
    state       TEXT NOT NULL,
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);
```

#### Column definitions

| Column | Description | Example |
|--------|-------------|---------|
| `id` | Unique sandbox identifier | `sbx1` |
| `n` | Sandbox sequence number | `1` |
| `ip` | Sandbox IP address | `10.201.0.1` |
| `pid` | Firecracker process ID | `12345` |
| `state` | Current sandbox lifecycle state | `running` |
| `created_at` | Creation timestamp (Unix seconds) | `1794470400` |
| `updated_at` | Last update timestamp (Unix seconds) | `1794470405` |

#### Supported states

For Milestone 1G:

```text
starting → running → destroyed
```

Additional states planned for Milestone 3:

```text
pausing
paused
resuming
```

### Sandbox lifecycle management

#### Creating a sandbox

When receiving:

```http
POST /sandboxes
```

Execute the following sequence:

1. Generate a unique sandbox ID and allocate its metadata.
2. Insert a row into SQLite with `state = 'starting'`.
3. Call the Node Agent's `create_sandbox()`.
4. Wait for successful sandbox creation.
5. Update SQLite with `state = 'running'`.
6. Return the sandbox ID and state to the client.

**Important:** Persist the `starting` state before initiating sandbox creation.

#### Destroying a sandbox

When receiving:

```http
DELETE /sandboxes/{id}
```

1. Look up the sandbox in SQLite.
2. Call `destroy_sandbox()` to clean up its resources.
3. Update the sandbox state to `destroyed`.
4. Return `{"ok": true}`.

`starting` is a real state. Delete tears down a sandbox that is still `starting`, the same as one that is `running`. A sandbox that is already `destroyed` is left alone, and that call still returns `{"ok": true}`.

#### Recovering sandboxes on server startup

When FastAPI starts:

1. Query SQLite for all sandboxes whose state is not `destroyed`.
2. Check whether each sandbox's Firecracker PID is still alive.
3. If the process is alive, retain its state and make it available for subsequent operations.
4. If the process is dead:
   - Clean up remaining sandbox resources.
   - Update its database state to `destroyed`.

**Goal:** Restarting the API server must not destroy healthy Firecracker MicroVMs.

### Start the API server

Run the API on the Linux host:

```bash
sudo .venv/bin/uvicorn api.main:app --host 0.0.0.0 --port 8000
```

The server listens on port `8000`.

#### Verify API availability

```bash
curl -i http://localhost:8000/sandboxes
```

Expected result:

```text
HTTP/1.1 200 OK
```

The response should contain the sandbox list, initially `[]` if no sandboxes exist.

### End-to-end testing

Use `curl` to verify all API endpoints.

#### Test 1: Create sandbox

```bash
curl -X POST http://localhost:8000/sandboxes
```

Expected:

```json
{
  "id": "sbx1",
  "state": "running"
}
```

✅ PASS: Sandbox created successfully.

❌ FAIL: Sandbox creation fails or returns an invalid response.

#### Test 2: List sandboxes

```bash
curl http://localhost:8000/sandboxes
```

✅ PASS: Newly created sandbox appears in the list.

❌ FAIL: Sandbox is missing or has an incorrect state.

#### Test 3: Get sandbox details

```bash
curl http://localhost:8000/sandboxes/sbx1
```

✅ PASS: Returns sandbox ID, state, and creation timestamp.

❌ FAIL: Sandbox metadata is missing or incorrect.

#### Test 4: Execute command

```bash
curl -X POST http://localhost:8000/sandboxes/sbx1/exec \
  -H "Content-Type: application/json" \
  -d '{"cmd":"uname -a"}'
```

✅ PASS: Command executes inside the MicroVM and returns the Guest Agent's result.

❌ FAIL: Command fails, times out, or executes on the host instead of inside the MicroVM.

#### Test 5: Write file

```bash
curl -X PUT \
  "http://localhost:8000/sandboxes/sbx1/files?path=/tmp/hello.txt" \
  -H "Content-Type: text/plain" \
  --data-binary "Hello from FastAPI"
```

Expected:

```json
{
  "ok": true
}
```

✅ PASS: File successfully written inside the MicroVM.

❌ FAIL: File is missing or write operation fails.

#### Test 6: Read file

```bash
curl \
  "http://localhost:8000/sandboxes/sbx1/files?path=/tmp/hello.txt"
```

Expected:

```text
Hello from FastAPI
```

✅ PASS: File content matches the written data.

❌ FAIL: File cannot be read or content differs.

#### Test 7: Destroy sandbox

```bash
curl -X DELETE http://localhost:8000/sandboxes/sbx1
```

Expected:

```json
{
  "ok": true
}
```

✅ PASS: Sandbox resources are removed and SQLite state is updated.

❌ FAIL: Firecracker continues running or resources are not cleaned up.

#### Test 8: Unknown sandbox

```bash
curl -i http://localhost:8000/sandboxes/unknown
```

✅ PASS: Returns HTTP `404`.

❌ FAIL: Returns a successful status or an unexpected server error.

### Server restart and state recovery

Verify that SQLite persistence allows the API server to recover running sandboxes.

#### Procedure

1. Create a sandbox using `POST /sandboxes`.
2. Confirm its state is `running`.
3. Stop the Uvicorn server using `Ctrl+C`.
4. Verify that the Firecracker process remains alive.
5. Restart the API server:

```bash
sudo .venv/bin/uvicorn api.main:app --host 0.0.0.0 --port 8000
```

6. List the sandboxes:

```bash
curl http://localhost:8000/sandboxes
```

7. Execute a command inside the previously created sandbox:

```bash
curl -X POST http://localhost:8000/sandboxes/sbx1/exec \
  -H "Content-Type: application/json" \
  -d '{"cmd":"echo recovered"}'
```

#### Expected results

✅ PASS: Existing sandbox still appears as `running`.

✅ PASS: Sandbox accepts new commands after the API restart.

✅ PASS: Previously created MicroVM was not recreated or destroyed.

❌ FAIL: Running sandbox disappears from SQLite.

❌ FAIL: API restart terminates the Firecracker process.

❌ FAIL: Sandbox appears as running but cannot accept commands.

### Definition of done

Milestone 1G is complete when:

#### API functionality

- [ ] `POST /sandboxes` creates a sandbox.
- [ ] `GET /sandboxes` lists all sandboxes.
- [ ] `GET /sandboxes/{id}` returns sandbox details.
- [ ] `POST /sandboxes/{id}/exec` executes commands.
- [ ] `GET /sandboxes/{id}/files` reads files.
- [ ] `PUT /sandboxes/{id}/files` writes files.
- [ ] `DELETE /sandboxes/{id}` destroys sandboxes.

#### Error handling

- [ ] Unknown sandbox IDs return HTTP `404`.
- [ ] Operations requiring a running sandbox return HTTP `409` when it is not running.

#### State persistence

- [ ] Sandbox metadata is stored in `state.db`.
- [ ] Sandboxes are recorded as `starting` before creation.
- [ ] Successfully created sandboxes transition to `running`.
- [ ] Destroyed sandboxes transition to `destroyed`.
- [ ] Server startup checks non-destroyed sandbox PIDs.
- [ ] Dead sandboxes are cleaned up and marked `destroyed`.

#### Integration testing

- [ ] Every endpoint works using `curl`.
- [ ] Running sandboxes survive API server restarts.
- [ ] Recovered sandboxes remain accessible and usable.
- [ ] Sandbox commands execute inside the Guest VM, not on the host.

#### Milestone completion criteria

**Milestone 1G is complete when the FastAPI server can create, manage, and interact with Firecracker sandboxes through HTTP, while persisting their state across API server restarts.**

### 1G tradeoffs

#### Recovery checks only the PID

Checking whether a PID exists is a minimum recovery check. A production implementation should also verify process identity and sandbox connectivity.

#### Binding to `0.0.0.0`

The API controls privileged host operations. Binding to `0.0.0.0` exposes it on all host interfaces. Restrict network access and add authentication before allowing untrusted clients to connect.

#### Staying on httpx 0.28

Pytest prints one warning: Starlette's test client still imports the `httpx` package, and that import is deprecated in favor of `httpx2` (httpx 2.0). The tests still pass. We are staying on the current httpx (`0.28.1`) for now. Moving to 2.0 can wait.

---

## Steps 1H/I: SDK and example agents

An example coding agent that uses OpenAI to complete tasks inside a sandbox, through the Python SDK in `sdk/`. For setup, see [Connecting from your computer](#connecting-from-your-computer).

### Running the agents

Run the one-shot agent:

```bash
uv run python -m example_agent.agent_openai "create hello.py that prints hello, and run it"
```

Or start the client agent, an interactive chat that keeps one sandbox open across tasks:

```bash
# must be run as a module from sbx/
uv run python -m client.client
```

Type a task at the `>` prompt. The agent works on it in the sandbox and prints its answer. Type `/quit` to exit; the sandbox is destroyed automatically. An example session (the agent's exact steps and wording will vary):

```
sandbox sbx1 ready
> create hello.py that prints hello, and run it
[write] hello.py
[run] python hello.py
      hello
I created hello.py and ran it; it printed "hello".
done
> /quit
```
```
sandbox sb1 ready
> clone https://github.com/kiyashahidi9/example-express-app and start the dev server
> curl the health check
> add a /time route that returns the current time, then curl it
> make the time return in EST
> /quit
```

### Telling the agent where it works

The sandbox doesn't tell the agent which directory it's in. The client app has to say so, either in the system prompt or in the tool descriptions. If it doesn't, the agent loses track of its own files.

Commands run in `/root` by default, so relative paths work in `run_command`. The guest agent's file endpoints, though, only accept absolute paths. An agent that doesn't know about `/root` guesses one. In testing, the agent cloned `example-express-app` into `/root` and then kept trying to read `/sandbox/example-express-app/index.js`, which got a 404. Its relative-path attempts got a 400 because they weren't absolute. It gave up without ever finding the file.

The client (`client/client.py`) handles this with a single `WORKDIR` setting, used in three places:

- `build_tools(WORKDIR)` (`client/tools.py`) puts the working directory in each tool's description.
- `run_command` runs with `cwd=WORKDIR`, so commands run where the descriptions say they do.
- `absolute_path()` joins relative `read_file`/`write_file` paths onto `WORKDIR`.

Any other client should do the same. Tell the model the working directory, through the system prompt, the tool descriptions (`client/tools.py`), or both. Make sure the directory it's told about is the one commands actually run in. If you change `WORKDIR`, that folder must already exist in the VM, or `exec` returns a 400.

### 1H/I tradeoffs

How to tell the agent where it works (for the case study):

| Approach | Pro | Con |
|---|---|---|
| System prompt | One sentence; the model sees it from the start | Easy to drift from the real `cwd`; every client must remember to add it |
| Tool description | The rule sits on the tool, right where the model picks a path | Repeated in every tool schema; still only guidance |
| Client resolves relative paths | No model cooperation needed | Can't fix a made-up absolute path like `/sandbox/...` |
| Tool results report `cwd` / errors name it | Agent recovers from its own mistakes | Only helps after a failed call |
| Absolute-paths-only API (current) | Unambiguous on the server side | Pushes the problem onto every client |

Our choice: tool descriptions plus client-side resolution, both driven by one `WORKDIR`. The system prompt stays generic. A system-prompt line and better error messages are cheap additions if agents still guess wrong.

---

## To revisit

- [ ] **`create_network()` and `scripts/netns_create.sh`.** Go back and understand what the script sets up step by step (namespace, `tap0`, veth pair, SNAT/DNAT rules, host route). Both scripts now exist, copied from Build Plan 1d; `netns_delete.sh` also clears the host's connection tracking for the slot and fails if anything is left.
- [ ] **Host setup script (GCP only).** `node_agent.py` downloads nothing; it assumes Firecracker, `vmlinux` and `rootfs.ext4` (with the guest agent) are already in place. Write a script that downloads and installs these on a GCP VM, then save it as an image so a new node is one `gcloud` command.
- [ ] **Hosting tradeoff: GCP only vs AWS vs any machine.** Letting clients pick their own machine (we handle virtualization setup) reaches more clients, e.g. those who want to avoid GCP or run on their laptop. Downside: their OS and hardware become our problem. Old Linux kernels, missing KVM permissions or hardware support can make Firecracker refuse to run, and macOS has no KVM at all. That's harder to support and debug.
- [ ] **Firecracker setup calls:** `set_boot_source()`, `attach_root_drive()`, `set_machine_config()`, `attach_network()`. Each sends one of the 1c `curl` calls to Firecracker. Review what each setting means and why we chose its values (boot args, read-only vs writable disk, vCPUs/memory, MAC → guest IP).

## Stretch goals

- [ ] **Extract our code into importable layers**, bottom-up, after Milestone 3, once the pause/snapshot/load calls are known:
  1. **Firecracker client** (`firecracker_client.py`): only Firecracker's API (boot source, drive, machine config, network, start, later pause/snapshot/load). Takes plain values (socket path, disk path), no `sandbox_id`.
  2. **Sandbox host layer** (`node_agent.py`): numbering, folders, namespaces, cgroups, cleanup, built on the Firecracker client.

  **Why:** clearer boundaries, each layer testable on its own, easier for outside engineers to read. Keep them as modules in the repo; publishing to PyPI isn't worth it for the capstone.
- [ ] **Scale horizontally to more than one host.** One host caps us at ~20 sandboxes (RAM) and 250 slots (IP). Add more GCP machines, each running its own VMs and slots, and move the DB off-host (e.g. Postgres/Cloud SQL) so the API knows which host holds each sandbox.
