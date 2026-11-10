# Persistent Sandboxes: Design Notes

Short record of design decisions in `node_agent.py` and why we made them.

## 1. Two identifiers: `sandbox_id` and `n`

Both name the same sandbox: `n = 3` ⇄ `sandbox_id = "sbx3"`.

| | `n` (int) | `sandbox_id` (str) |
|---|---|---|
| Used for | Addresses: `10.201.0.3`, `10.200.3.x`, network script args | Names: folders, cgroup, namespace, API URLs, DB key |
| Why | The IP scheme needs a small integer (1–250) | A readable public name whose format can change later |

**Rule:** public functions take only `sandbox_id`. Code that needs the number calls `number_from_id()`, which also rejects bad IDs (e.g. `"../etc"`) before they're used in paths we delete.

**Tradeoff:** storing both in `Sandbox` and the DB is redundant, but saves converting everywhere. `id` is always `sbx<n>`.

## 2. Where commands run

| Where | How | Example |
|---|---|---|
| Host | `subprocess.run` | `cp`, `netns_create.sh 3` |
| Host, inside namespace `sbx3` | `ip netns exec sbx3 ...` | Firecracker |
| Inside VM `sbx3` | HTTP to the guest agent | `exec("sbx3", "npm install")` |

- Host commands inherit Python's user (root), environment and working folder. Scripts are run by full path, so the working folder doesn't matter.
- The VM is a separate computer, so the host can't run programs in it directly. That's why the guest agent exists.

**Tradeoff:** we call our tested 1d bash scripts instead of rewriting networking in Python (e.g. `pyroute2`). That means less new code, but errors come back as script output instead of Python exceptions, so `run_script` includes that output in the error.

## 3. Picking a sandbox number: `claim_free_number()`

> **Open: we'll come back to this.** Revisit the UUID question and lower `MAX_SANDBOX_NUMBER` (now 250) to what the host can actually run (~20 at 1 GiB per VM).

Gives each new sandbox the **lowest unused number** `n` (1, 2, 3, …). The number is needed for the VM's network addresses, not just as a unique name.

- **Claim = create the folder.** `os.mkdir("/var/sbx/sbx<n>")` checks and creates in one step, so two requests at once can't get the same number.
- **Freed numbers are reused**, so we never run out of IPs (`10.201.0.<n>` can't go above 254).
- **Why not a UUID?** It works as a public name, but the network still needs a small number (IP addresses, and Linux interface names max out at 15 characters). We could use both later: a UUID for the public ID plus `n` for networking.

**Tradeoff:** reusing numbers means a destroyed sandbox's old ID (`sbx3`) can later point to a new sandbox. That's fine for the MVP; switch the public ID to a UUID once outside clients hold on to IDs.

## 4. cgroups: one meter per sandbox

A **cgroup** (control group) is a Linux group of processes whose CPU and memory use can be **measured** (and later **limited**). You work with it through special folders that Linux fills in live:

| Action | How |
|---|---|
| Create a group | `mkdir /sys/fs/cgroup/sandboxes/sbx3` (parent folder = `CGROUP_ROOT`) |
| Add a process | Write its PID to `.../sbx3/cgroup.procs` |
| Read CPU use | `.../sbx3/cpu.stat` |

**Why:** each sandbox's Firecracker process gets its own group, so Milestone 2's sampler can see how busy each VM is **from the host**, with no code inside the VM.

**Order matters:** a cgroup can only be removed once its process is gone, so `destroy_sandbox` kills Firecracker first.

## 5. One sandbox = one Firecracker process

```
Host (GCP machine)
├── python (node agent / API server)     PID 1000
├── firecracker  ← sandbox sbx1          PID 2041
├── firecracker  ← sandbox sbx2          PID 2077
└── firecracker  ← sandbox sbx3          PID 2103
```

That one process **is** the whole VM. Everything inside it (guest agent, `npm`, dev servers) is hidden inside that PID.

- **Kill the PID → the whole sandbox stops.**
- **One cgroup per sandbox** measures everything in the VM.
- **The PID is saved** (`firecracker.pid`), so a restarted node agent can still find and kill it.
- **The PID changes on resume** (Milestone 3): pause kills the process, resume starts a new one. The sandbox ID stays the same.

## To revisit

- [ ] **`create_network()` and `scripts/netns_create.sh`.** Go back and understand what the script sets up step by step (namespace, `tap0`, veth pair, SNAT/DNAT rules, host route). Note: the script doesn't exist yet. 1d built one `netns.sh` whose `start` also boots a VM, so we need a network-only `netns_create.sh` (and `netns_delete.sh`).
- [ ] **Firecracker setup calls:** `set_boot_source()`, `attach_root_drive()`, `set_machine_config()`, `attach_network()`. Each sends one of the 1c `curl` calls to Firecracker. Review what each setting means and why we chose its values (boot args, read-only vs writable disk, vCPUs/memory, MAC → guest IP).

## Stretch goals

- [ ] **Extract our code into importable layers**, bottom-up, after Milestone 3, once the pause/snapshot/load calls are known:
  1. **Firecracker client** (`firecracker_client.py`): only Firecracker's API (boot source, drive, machine config, network, start, later pause/snapshot/load). Takes plain values (socket path, disk path), no `sandbox_id`.
  2. **Sandbox host layer** (`node_agent.py`): numbering, folders, namespaces, cgroups, cleanup, built on the Firecracker client.

  **Why:** clearer boundaries, each layer testable on its own, easier for outside engineers to read. Keep them as modules in the repo; publishing to PyPI isn't worth it for the capstone.
