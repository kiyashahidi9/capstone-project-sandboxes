# Persistent Sandboxes: Design Notes

Short record of design decisions in `node_agent.py` and why we made them.

## 1. Two identifiers: `sandbox_id` and `n`

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

**Tradeoff:** one extra lookup per call (an indexed SQLite query), and two identifiers to keep straight. Long IDs are harder to read in logs; print the first 8 characters if needed.

## 2. Where commands run

| Where | How | Example |
|---|---|---|
| Host | `subprocess.run` | `cp`, `netns_create.sh 3` |
| Host, inside namespace `sbx3` (slot 3) | `ip netns exec sbx3 ...` | Firecracker |
| Inside the VM | HTTP to the guest agent | `exec("sbx_3f9c…", "npm install")` |

- Host commands inherit Python's user (root), environment and working folder. Scripts are run by full path, so the working folder doesn't matter.
- The VM is a separate computer, so the host can't run programs in it directly. That's why the guest agent exists.

**Tradeoff:** we call our tested 1d bash scripts instead of rewriting networking in Python (e.g. `pyroute2`). That means less new code, but errors come back as script output instead of Python exceptions, so `run_script` includes that output in the error.

## 3. Picking a slot: `claim_free_slot()`

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

### How we use SQLite safely

| Rule | Why |
|---|---|
| **Each statement is atomic on its own**, so single statements need no `BEGIN … COMMIT` | SQLite wraps every statement in a transaction. Our three functions are one statement each |
| Use `BEGIN IMMEDIATE … COMMIT` when **a read decides a write**, or **two writes must stay consistent** | Otherwise two callers can both read "slot 3 is free" (a check-then-act race), or a crash between two writes leaves them disagreeing. `IMMEDIATE` takes the write lock up front, so a second caller waits instead of failing |
| Open with `isolation_level=None` | Turns off Python's hidden transactions, so each statement commits on its own and any `BEGIN` is ours |
| `timeout=5` and `PRAGMA journal_mode=WAL` | A second writer waits up to 5 s instead of failing with "database is locked"; readers aren't blocked while someone writes |
| **A new connection per call** | The API server calls us from several threads, and a SQLite connection shouldn't be shared between threads |

## Decisions about sandbox IDs (and the tradeoffs)

### Decision 1: UUID IDs instead of `sbx<n>`

| | `sbx<n>` (before) | `sbx_<uuid4>` + separate slot `n` (now) |
|---|---|---|
| An old ID can reach a new sandbox | **Yes**: slot numbers are reused, so names were too | No: IDs are never reused |
| Works as a key for the DB (1g), events (M5), snapshots (M6) | No: collisions after reuse | Yes |
| Finding the slot | Parse it out of the name | One lookup |
| Readability | `sbx3` | `sbx_3f9c1a7e…` (print the first 8 characters) |

**Why `uuid4`:** Python 3.11 has no `uuid7`, and at our scale ID ordering doesn't matter. Short random IDs (e.g. 4 hex characters) collide after a few hundred sandboxes; auto-increment integers are guessable and still need a separate slot.

### Decision 2: where to record which sandbox holds which slot

We considered three options. Each claims a slot atomically, so all three are safe when two sandboxes are created at once:

| | Folder `/var/sbx/sbx<n>` (`mkdir`) | Symlink `slots/<n> -> <id>` | **SQLite `slots` table (chosen)** |
|---|---|---|---|
| Atomic claim | ✅ `mkdir` fails if it exists | ✅ `symlink` fails if it exists | ✅ `PRIMARY KEY` rejects duplicates |
| Records the owner | ❌ (only worked while ID = `sbx<n>`) | ✅ in the link's text | ✅ `sandbox_id` column |
| Owner check on release | — | Read, compare, then delete | Built into the `DELETE` |
| Find a sandbox's slot | — | Scan up to 250 links | One indexed query |
| One record shared with 1g | ❌ | ❌ The API compares two places on startup | ✅ 1g's `sandboxes` table lives in the same file |
| Setup | None | None | A DB file (`sqlite3` ships with Python) |
| Thread care | None | None | A connection per call, wait timeout, WAL |
| Inspect it | `ls /var/sbx` | `ls -l /var/sbx/slots` | `sqlite3 /var/sbx/state.db "SELECT * FROM slots"` |

**Why SQLite:** 1g needs a database anyway, so keeping slots there gives one source of truth instead of two records compared on startup, plus indexed lookups and a built-in owner check. The cost is a little setup and thread care, listed above.

**What we did *not* gain:** neither a symlink nor a DB row can see the kernel. Either can say a slot is free while `veth3h` still exists (e.g. after a crash or a manual delete). What keeps slots safe is the same in every option: **free the slot last**, and `netns_delete.sh` **fails if anything is left**, so a dirty slot is never handed out.

**Considered, not chosen: the API picks the slot.** 1g could choose `n` and pass it to `create_sandbox(id, n)`. That also gives one source of truth, but the node agent couldn't run on its own (the 1f tests would need the API). Keeping the claim in the node agent, in a database file 1g shares, gives us both.

## 4. cgroups: one meter per sandbox

A **cgroup** (control group) is a Linux group of processes whose CPU and memory use can be **measured** (and later **limited**). You work with it through special folders that Linux fills in live:

| Action | How |
|---|---|
| Create a group | `mkdir /sys/fs/cgroup/sandboxes/<sandbox_id>` (parent folder = `CGROUP_ROOT`) |
| Add a process | Write its PID to `.../<sandbox_id>/cgroup.procs` |
| Read CPU use | `.../<sandbox_id>/cpu.stat` |

**Why:** each sandbox's Firecracker process gets its own group, so Milestone 2's sampler can see how busy each VM is **from the host**, with no code inside the VM.

**Order matters:** a cgroup can only be removed once its process is gone, so `destroy_sandbox` kills Firecracker first.

## 5. One sandbox = one Firecracker process

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

## 6. What the API server (1g) gets from here

| 1g needs | Node agent gives it |
|---|---|
| Save the DB row as `starting` *before* booting | `new_sandbox_id()`, then `create_sandbox(sandbox_id)` |
| 404 for an unknown sandbox | `exec` / `read_file` / `write_file` raise `LookupError` (unknown) or `ValueError` (malformed ID) |
| On startup: is each DB sandbox still alive? | `is_sandbox_running(id)` |
| On startup: anything on the host the DB doesn't know? | `list_host_sandboxes()` (then `destroy_sandbox` each orphan) |
| Restart the server without killing VMs | Firecracker starts in its own session, so Ctrl+C on uvicorn doesn't reach it |

**Still 1g's job:** its own `sandboxes` table (state, pid, timestamps) in the **same** `state.db`. It shouldn't store `n` again: `JOIN slots ON slots.sandbox_id = sandboxes.id` or `find_slot(id)` gives the slot, so there is only one copy. Also one lock per sandbox, so `exec` and `destroy` for the same ID can't overlap.

**Tradeoff:** `delete_network()` always runs `netns_delete.sh` instead of first checking the namespace exists. It's one extra script call per destroy, but it also cleans up half-built networks (e.g. a host route left without its namespace), and the script fails if anything for the slot remains, so the slot is never handed out dirty.

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
