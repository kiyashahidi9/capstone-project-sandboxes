# Track C: pause and resume

Pausing saves a sandbox's whole VM (memory, running processes, disk) to files and stops it, so it uses no RAM. Resuming brings it back exactly where it left off.

```text
running → pausing → paused → resuming → running
any state → destroyed            (anything else answers 409)
```

| Call | What it does |
| -- | -- |
| `POST /sandboxes/{id}/pause` | Cuts off the agent's turn, saves the VM, and frees its memory (~15 s) |
| `POST /sandboxes/{id}/resume` | Brings a paused sandbox back (~1–2 s) |
| `exec`, `GET/PUT files` | Resume a paused sandbox first, then do the request |
| SDK | `sbx.pause()`, `sbx.resume()` |

# Decisions

### How we stop two changes from clashing

We use **both** a lock and the in-between states (`pausing`, `resuming`), because they do different jobs.

| | The lock | The in-between states |
| -- | -- | -- |
| A second request | **waits** its turn | is **turned away** (409) |
| Who can see it | only this server process | anything that reads the database |
| Survives a crash | no | yes |

- The lock is what lets "resume during a pause" wait, then resume.
- The states block a wrong change even without the lock, show the truth on `GET`, and mark which sandboxes a crash interrupted.
- Every state change is one SQL statement that checks and writes at the same time (`transition()`), so two requests can't both pass the check.

### The decisions, and what each costs

| Question | Decision | Tradeoff |
| -- | -- | -- |
| Kind of lock | **`threading.Lock`**, one per sandbox (`locks.lock_for()`) | Matches the server and the agent runner, which both use threads. Each waiting request ties up one thread. |
| Exec or file call on a **running** sandbox | **Don't take the lock** | The agent's turn holds the lock while the agent itself calls exec, so taking it would make the agent wait on itself. Cost: an exec that lands just as a pause starts may get a 502. |
| Resume on a sandbox that **isn't paused** | **Answer 409 at once**, without waiting for the lock | Otherwise it could wait up to 30 min behind an agent turn, just to be told no. Safe, because `transition()` checks again under the lock. |
| A new turn sneaks in before pause gets the lock | **Pause keeps cutting off turns** until it has the lock (retries every 1 s) | A pause can't get stuck behind a fresh 30-minute turn. |
| Should pause wait until done? | **Yes**: one request, ~15 s | Simple. Returning at once and making clients check back would be more code. |
| Pause or resume twice | **409**, as the spec says | Unlike `DELETE`, which is fine to repeat. Documented in the SDK. |
| Timings in the response | **No**, just the sandbox | Simpler API. Measure timings with `node_agent` directly. |
| Delete during a pause | **Waits** for the pause to finish, then destroys | Up to ~15 s slower, but never leaves half-paused leftovers. |
| A pause or resume fails | **Record where the VM really ended up**: `running` if it answers, `paused` if the snapshot is complete, else `destroyed` | A few checks, but the database never claims something the host doesn't match. |
| Server crashes mid-pause/resume | **Fix it on startup** with the same checks | Without this, the sandbox would be stuck answering 409 forever. |
| Changing the table's allowed states | **Rebuild the table on startup** when it differs from `schema.sql`, copying every shared column | Any old database upgrades by itself, and Track A's columns (e.g. `turn_state`) survive the merge. |
| Where the table is defined | **Only in `db/schema.sql`**; the rebuild reads it from there | One copy to edit. The rebuild renames the old table first, which is only safe while nothing else points at `sandboxes`. |
| 2e (agent runner) isn't merged yet | **Temporary stand-ins** with the same names: `sbx/locks.py` and a do-nothing `stop_turn()` in `sbx/runner/runner.py` | 4c works today. Swap in Track A's files on merge; nothing else changes. |
| Testing | **Unit tests with a fake host**, plus one manual run on the GCP host | The fakes cover every path quickly; only the host run proves real Firecracker. |

### To revisit

- [ ] **On merge with Track A:** keep their `locks.py` and `runner/runner.py`, combine the `sandboxes` table in `schema.sql`, and merge `SandboxRow` and `SELECT_SANDBOXES`.
- [ ] **New `NOT NULL` columns need a default**, or the table rebuild fails on older databases.
- [ ] **Exec during a pause:** if the 502s happen in practice, use a read-write lock (exec shares it, pause waits for running execs to finish).
- [ ] **Long pause requests:** if ~15 s requests cause trouble (proxies, many pauses at once), return straight away and let clients check back.
- [ ] **More than one server process:** the lock only works inside one process. With several workers, only the database states protect a sandbox.
- [ ] **The full startup check** (destroying sandboxes whose VM died) must skip `paused` sandboxes. They have no VM on purpose.
- [ ] **One snapshot folder per pause (Milestone 7):** today every pause reuses `snap/`. With bucket uploads, use `snap/<snapshot_id>/` so an old file can never pass for a new one.

# Experiment results (4a, 4b)

Host `sbx-node-1` (n2-standard-8, Firecracker v1.18.0); VM with 2 vCPUs and 1 GiB.

| Measurement | Result |
| -- | -- |
| Pause (snapshot write) | **9–15 s**: the whole 1 GiB is written, whatever the VM used, so the disk's speed sets it |
| Resume, cold / warm cache | **~2 s** / ~0.2 s |
| Snapshot files | `mem` 1 GiB (mostly zeros), `vmstate` 24 KB |
| RAM freed by a pause | What the VM actually used (~130 MB), not its 1 GiB |
| Running programs and network | Kept going after resume: counter, dev server, DNS |
| Guest clock after resume | Behind by the length of the pause (needs the `/clock` fix) |

**Lessons**

- `mem`, `vmstate` and the disk are one set. Mixing files from different pauses corrupts the filesystem without any error at first.
- Compress `mem` before uploading it (Milestone 7).
- To time a cold resume, drop the cache first: `echo 3 > /proc/sys/vm/drop_caches`.
- Firecracker must not share the terminal (`stdin=subprocess.DEVNULL`), or your typing goes into the VM.
- After restarting Firecracker by hand, update `firecracker.pid` and `cgroup.procs`, or delete can't clean it up.
- Destroy any sandbox left over from a manual check before running the host tests.
