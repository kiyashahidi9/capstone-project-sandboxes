import os
import sqlite3
import threading
from contextlib import closing, nullcontext

import httpx
import pytest

import host_manager


ID_A = "sbx_" + "a" * 32
ID_B = "sbx_" + "b" * 32


# --- names and IDs ----------------------------------------------------------

def test_new_ids_are_valid_and_unique():
    ids = {host_manager.new_sandbox_id() for _ in range(1000)}
    assert len(ids) == 1000
    for sandbox_id in ids:
        host_manager.validate_sandbox_id(sandbox_id)   # does not raise


def test_names_built_from_slot_number():
    assert host_manager.namespace_for(7) == "sbx7"
    assert host_manager.guest_ip_for(7) == "10.201.0.7"


@pytest.mark.parametrize("bad_id", [
    "", "sbx", "sbx3",                       # old format is no longer accepted
    "sbx_" + "a" * 31, "sbx_" + "A" * 32,    # wrong length, uppercase
    "sbx_" + "a" * 32 + "/..", "../etc", "state.db",
])
def test_validate_rejects_bad_ids(bad_id):
    with pytest.raises(ValueError):
        host_manager.validate_sandbox_id(bad_id)


# --- slots (SQLite) -------------------------------------------------------------

@pytest.fixture
def fake_sandboxes_root(tmp_path, monkeypatch):
    """Point SANDBOXES_ROOT (and so the state database) at a temporary folder instead of /var/sbx."""
    root = str(tmp_path)          # pytest gives a Path; we use plain strings
    monkeypatch.setattr(host_manager, "SANDBOXES_ROOT", root)
    return root


def insert_slot(n, sandbox_id):
    """Mark slot n as taken by sandbox_id directly in the database (test setup)."""
    with closing(host_manager.open_db()) as conn:
        conn.execute("INSERT INTO slots (n, sandbox_id) VALUES (?, ?)", (n, sandbox_id))


def test_claims_lowest_free_slot(fake_sandboxes_root):
    insert_slot(1, "sbx_" + "1" * 32)
    insert_slot(3, "sbx_" + "3" * 32)
    assert host_manager.claim_free_slot(ID_A) == 2
    assert host_manager.find_slot(ID_A) == 2
    assert host_manager.claim_free_slot(ID_B) == 4


def test_raises_when_all_slots_taken(fake_sandboxes_root, monkeypatch):
    monkeypatch.setattr(host_manager, "MAX_SANDBOX_NUMBER", 2)
    host_manager.claim_free_slot(ID_A)
    host_manager.claim_free_slot(ID_B)
    with pytest.raises(host_manager.NoFreeSlotsError, match="in use"):
        host_manager.claim_free_slot("sbx_" + "c" * 32)


def test_a_sandbox_cannot_hold_two_slots(fake_sandboxes_root):
    host_manager.claim_free_slot(ID_A)
    with pytest.raises(sqlite3.IntegrityError):        # sandbox_id is UNIQUE
        insert_slot(5, ID_A)


def test_concurrent_claims_never_share_a_slot(fake_sandboxes_root):
    """20 threads claim at the same moment: every one gets a different slot."""
    ids = [f"sbx_{i:032x}" for i in range(20)]
    start = threading.Barrier(len(ids))
    results = {}

    def claim(sandbox_id):
        start.wait()                                   # release all threads at once
        results[sandbox_id] = host_manager.claim_free_slot(sandbox_id)

    threads = [threading.Thread(target=claim, args=(i,)) for i in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results.values()) == list(range(1, 21))   # 20 different slots, no gaps


def test_find_slot(fake_sandboxes_root):
    assert host_manager.find_slot(ID_A) is None          # empty database
    n = host_manager.claim_free_slot(ID_A)
    assert host_manager.find_slot(ID_A) == n
    assert host_manager.find_slot(ID_B) is None


def test_freed_slot_is_reused_under_a_new_id(fake_sandboxes_root):
    """The whole point of the change: slot numbers are reused, IDs are not."""
    n = host_manager.claim_free_slot(ID_A)
    host_manager.release_slot(n, ID_A)
    assert host_manager.claim_free_slot(ID_B) == n
    assert host_manager.find_slot(ID_A) is None          # old ID no longer reaches slot n


def test_release_does_not_free_another_sandboxes_slot(fake_sandboxes_root):
    n = host_manager.claim_free_slot(ID_B)
    host_manager.release_slot(n, ID_A)                   # A doesn't hold it
    assert host_manager.find_slot(ID_B) == n
    host_manager.release_slot(99, ID_A)                  # missing slot: no error


def test_require_slot_raises_lookup_error_for_unknown_sandbox(fake_sandboxes_root):
    with pytest.raises(LookupError):
        host_manager.require_slot(ID_A)
    with pytest.raises(ValueError):
        host_manager.require_slot("../etc")


def test_sandbox_manager_url(fake_sandboxes_root):
    insert_slot(2, ID_A)
    assert host_manager.sandbox_manager_url(ID_A, "/health") == "http://10.201.0.2:49983/health"


# --- pid file and wait_until --------------------------------------------------

def test_pid_round_trip(fake_sandboxes_root):
    os.mkdir(host_manager.sandbox_dir(ID_A))
    assert host_manager.load_pid(ID_A) is None
    host_manager.save_pid(ID_A, 4242)
    assert host_manager.load_pid(ID_A) == 4242


def test_stale_pid_is_not_ours():
    # PID 1 (init) exists but is certainly not this sandbox's Firecracker.
    assert host_manager.pid_still_belongs_to_sandbox(1, ID_A) is False


def test_wait_until_times_out():
    with pytest.raises(TimeoutError, match="the thing"):
        host_manager.wait_until(lambda: False, timeout=0.1, interval=0.02, waiting_for="the thing")


# --- after a host reboot (6c): network and cgroup are wiped, the slot is not --------

def test_ensure_network_recreates_a_namespace_a_reboot_wiped(tmp_path, monkeypatch):
    """Checks a missing network namespace is created again for the sandbox's slot."""
    created = []
    monkeypatch.setattr(host_manager, "NETNS_DIR", str(tmp_path))
    monkeypatch.setattr(host_manager, "create_network", created.append)

    host_manager.ensure_network(3)

    assert created == [3]


def test_ensure_network_leaves_an_existing_namespace_alone(tmp_path, monkeypatch):
    """Checks a namespace that's still there isn't created twice."""
    created = []
    (tmp_path / "sbx3").touch()
    monkeypatch.setattr(host_manager, "NETNS_DIR", str(tmp_path))
    monkeypatch.setattr(host_manager, "create_network", created.append)

    host_manager.ensure_network(3)

    assert created == []


def test_ensure_cgroup_recreates_the_folders_a_reboot_wiped(tmp_path, monkeypatch):
    """Checks the sandbox's cgroup folder, and its parent, are made again; a second call changes nothing."""
    monkeypatch.setattr(host_manager, "CGROUP_ROOT", str(tmp_path / "sandboxes"))

    host_manager.ensure_cgroup(ID_A)
    host_manager.ensure_cgroup(ID_A)

    assert (tmp_path / "sandboxes" / ID_A).is_dir()


def skip_network_and_cgroup(monkeypatch):
    """Makes resume act as if the sandbox's network and cgroup are still there, so no real ones get made."""
    monkeypatch.setattr(host_manager, "ensure_network", lambda n: None)
    monkeypatch.setattr(host_manager, "ensure_cgroup", lambda sandbox_id: None)


def test_resume_recreates_network_and_cgroup_before_starting_the_vm(monkeypatch):
    """Checks resume rebuilds what a reboot wiped before Firecracker needs it."""
    calls = []
    monkeypatch.setattr(host_manager, "require_resumable_files", lambda sandbox_id: None)
    monkeypatch.setattr(host_manager, "require_stopped", lambda sandbox_id: None)
    monkeypatch.setattr(host_manager, "require_slot", lambda sandbox_id: 3)
    monkeypatch.setattr(host_manager, "remove_socket", lambda sandbox_id: None)
    monkeypatch.setattr(host_manager, "ensure_network", lambda n: calls.append("network"))
    monkeypatch.setattr(host_manager, "ensure_cgroup", lambda sandbox_id: calls.append("cgroup"))
    monkeypatch.setattr(host_manager, "kill_firecracker", lambda sandbox_id: None)

    def start_vm_process(sandbox_id, n):
        calls.append("start")
        raise RuntimeError("stop the test here")

    monkeypatch.setattr(host_manager, "start_vm_process", start_vm_process)

    with pytest.raises(RuntimeError, match="stop the test here"):
        host_manager.resume_sandbox(ID_A)
    assert calls == ["network", "cgroup", "start"]


# --- create_sandbox / destroy_sandbox ------------------------------------------

def test_create_sandbox_cleans_up_when_a_step_fails(fake_sandboxes_root, monkeypatch):
    """If any step fails, destroy_sandbox runs and the original error is raised."""
    destroyed = []
    monkeypatch.setattr(host_manager, "new_sandbox_id", lambda: ID_A)
    monkeypatch.setattr(host_manager, "ensure_cgroup_parent", lambda: None)
    monkeypatch.setattr(host_manager, "copy_base_image", lambda sandbox_id: None)

    def failing_network(n):
        raise RuntimeError("netns_create.sh failed")

    monkeypatch.setattr(host_manager, "create_network", failing_network)
    monkeypatch.setattr(host_manager, "destroy_sandbox", destroyed.append)

    with pytest.raises(RuntimeError, match="netns_create.sh failed"):
        host_manager.create_sandbox()
    assert destroyed == [ID_A]


def test_create_sandbox_uses_the_id_it_is_given(fake_sandboxes_root, monkeypatch):
    """1g makes the ID first (to save its DB row), then passes it in."""
    destroyed = []
    monkeypatch.setattr(host_manager, "ensure_cgroup_parent", lambda: None)
    monkeypatch.setattr(host_manager, "copy_base_image", lambda sandbox_id: None)
    def failing_network(n):
        raise RuntimeError("stop here")

    monkeypatch.setattr(host_manager, "create_network", failing_network)
    monkeypatch.setattr(host_manager, "destroy_sandbox", destroyed.append)

    with pytest.raises(RuntimeError, match="stop here"):
        host_manager.create_sandbox(ID_B)
    assert destroyed == [ID_B]


def test_create_sandbox_rejects_bad_or_reused_ids(fake_sandboxes_root, monkeypatch):
    monkeypatch.setattr(host_manager, "ensure_cgroup_parent", lambda: None)
    with pytest.raises(ValueError):
        host_manager.create_sandbox("../etc")
    os.mkdir(host_manager.sandbox_dir(ID_A))           # ID_A already exists on this host
    with pytest.raises(FileExistsError):
        host_manager.create_sandbox(ID_A)
    assert os.path.isdir(host_manager.sandbox_dir(ID_A))   # the existing sandbox was not touched


def test_destroy_frees_slot_only_after_network_is_deleted(fake_sandboxes_root, monkeypatch):
    """A new sandbox must not get the slot while the old one's veth still exists."""
    events = []
    monkeypatch.setattr(host_manager, "kill_firecracker", lambda sandbox_id: None)
    monkeypatch.setattr(host_manager, "remove_cgroup", lambda sandbox_id: None)
    monkeypatch.setattr(
        host_manager, "delete_network",
        lambda n: events.append(("network", n, host_manager.find_slot(ID_A))),
    )
    os.mkdir(host_manager.sandbox_dir(ID_A))
    n = host_manager.claim_free_slot(ID_A)

    host_manager.destroy_sandbox(ID_A)

    assert events == [("network", n, n)]               # slot still held while network was deleted
    assert host_manager.find_slot(ID_A) is None
    assert not os.path.exists(host_manager.sandbox_dir(ID_A))


def test_destroy_keeps_the_slot_if_network_delete_fails(fake_sandboxes_root, monkeypatch):
    """If the network can't be removed, the slot must stay taken so nobody collides with it."""
    monkeypatch.setattr(host_manager, "kill_firecracker", lambda sandbox_id: None)
    monkeypatch.setattr(host_manager, "remove_cgroup", lambda sandbox_id: None)

    def failing_delete(n):
        raise RuntimeError("netns_delete.sh failed")

    monkeypatch.setattr(host_manager, "delete_network", failing_delete)
    os.mkdir(host_manager.sandbox_dir(ID_A))
    n = host_manager.claim_free_slot(ID_A)

    with pytest.raises(RuntimeError):
        host_manager.destroy_sandbox(ID_A)
    assert host_manager.find_slot(ID_A) == n


def test_destroy_without_a_slot_skips_network(fake_sandboxes_root, monkeypatch):
    """create can fail before claiming a slot; destroy must still clean the folder."""
    monkeypatch.setattr(host_manager, "kill_firecracker", lambda sandbox_id: None)
    monkeypatch.setattr(host_manager, "remove_cgroup", lambda sandbox_id: None)
    monkeypatch.setattr(host_manager, "delete_network", lambda n: pytest.fail("no slot, no network"))
    os.mkdir(host_manager.sandbox_dir(ID_A))
    host_manager.destroy_sandbox(ID_A)
    assert not os.path.exists(host_manager.sandbox_dir(ID_A))


# --- for the API server (1g) ---------------------------------------------------

def test_is_sandbox_running_without_a_pid(fake_sandboxes_root):
    os.mkdir(host_manager.sandbox_dir(ID_A))
    assert host_manager.is_sandbox_running(ID_A) is False
    host_manager.save_pid(ID_A, 1)                       # PID 1 exists but isn't this sandbox's Firecracker
    assert host_manager.is_sandbox_running(ID_A) is False


def test_is_sandbox_running_rejects_bad_ids():
    with pytest.raises(ValueError):
        host_manager.is_sandbox_running("../etc")


def test_list_host_sandboxes(fake_sandboxes_root):
    assert host_manager.list_host_sandboxes() == []
    os.mkdir(host_manager.sandbox_dir(ID_A))             # has a folder
    host_manager.claim_free_slot(ID_B)                   # holds a slot but its folder is gone (orphan)
    os.mkdir(f"{fake_sandboxes_root}/sbx1")            # old-format leftover: not ours, ignored
    assert host_manager.list_host_sandboxes() == sorted([ID_A, ID_B])


# --- pause_sandbox / resume_sandbox (4b) ----------------------------------------

FULL_MEM_BYTES = host_manager.MEMORY_MIB * host_manager.BYTES_PER_MIB


def write_file_of_size(path, size):
    """Create a file of `size` bytes, making its folder if needed (test setup)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.truncate(size)                               # sparse, so a 1 GiB mem costs no real disk


def write_full_snapshot(sandbox_id):
    """Put a complete-looking snapshot and disk in place for sandbox_id (test setup)."""
    write_file_of_size(host_manager.vmstate_path(sandbox_id), 1)
    write_file_of_size(host_manager.mem_path(sandbox_id), FULL_MEM_BYTES)
    write_file_of_size(host_manager.disk_path(sandbox_id), 1)


class FakeFirecracker:
    """Stands in for Firecracker during pause tests, remembering every call it gets."""

    def __init__(self):
        self.calls = []
        self.killed = []
        self.mem_bytes_to_write = None                 # None: snapshot/create writes no files

    def put(self, client, path, body):
        """Remember a PUT; for snapshot/create, write the snapshot files if a size was set."""
        self.calls.append(("PUT", path))
        if path == "/snapshot/create" and self.mem_bytes_to_write is not None:
            write_file_of_size(body["snapshot_path"], 1)
            write_file_of_size(body["mem_file_path"], self.mem_bytes_to_write)

    def patch(self, client, path, body):
        """Remember a PATCH and the VM state it asks for."""
        self.calls.append(("PATCH", path, body["state"]))


@pytest.fixture
def fake_firecracker(fake_sandboxes_root, monkeypatch):
    """Swap the guest, Firecracker and kill calls that pause makes for a fake that records them."""
    fake = FakeFirecracker()
    monkeypatch.setattr(host_manager, "require_running", lambda sandbox_id: None)
    monkeypatch.setattr(host_manager, "sync_guest_disk", lambda sandbox_id: None)
    monkeypatch.setattr(host_manager, "firecracker_client", lambda sandbox_id: nullcontext())
    monkeypatch.setattr(host_manager, "firecracker_put", fake.put)
    monkeypatch.setattr(host_manager, "firecracker_patch", fake.patch)
    monkeypatch.setattr(host_manager, "kill_firecracker", fake.killed.append)
    return fake


def test_snapshot_paths(fake_sandboxes_root):
    assert host_manager.snapshot_dir(ID_A) == f"{fake_sandboxes_root}/{ID_A}/snap"
    assert host_manager.vmstate_path(ID_A) == f"{fake_sandboxes_root}/{ID_A}/snap/vmstate"
    assert host_manager.mem_path(ID_A) == f"{fake_sandboxes_root}/{ID_A}/snap/mem"


def test_pause_freezes_then_snapshots_then_kills(fake_firecracker):
    fake_firecracker.mem_bytes_to_write = FULL_MEM_BYTES
    timings = host_manager.pause_sandbox(ID_A)
    assert fake_firecracker.calls == [("PATCH", "/vm", "Paused"), ("PUT", "/snapshot/create")]
    assert fake_firecracker.killed == [ID_A]
    assert set(timings) == {"sync_s", "freeze_s", "snapshot_s", "kill_s"}


def test_pause_keeps_the_vm_running_if_no_snapshot_was_written(fake_firecracker):
    """No snapshot files: unfreeze the VM and never kill it, or the sandbox is lost."""
    with pytest.raises(RuntimeError, match="snapshot file missing"):
        host_manager.pause_sandbox(ID_A)
    assert fake_firecracker.calls[-1] == ("PATCH", "/vm", "Resumed")
    assert fake_firecracker.killed == []


def test_pause_keeps_the_vm_running_if_mem_is_too_small(fake_firecracker):
    """A cut-short mem file (the 4a gotcha) counts as a failed snapshot."""
    fake_firecracker.mem_bytes_to_write = FULL_MEM_BYTES - 1
    with pytest.raises(RuntimeError, match="mem file is"):
        host_manager.pause_sandbox(ID_A)
    assert fake_firecracker.calls[-1] == ("PATCH", "/vm", "Resumed")
    assert fake_firecracker.killed == []


def test_pause_never_passes_on_files_from_an_earlier_pause(fake_firecracker):
    """Old snapshot files are cleared first, so a pause that writes nothing still fails."""
    write_full_snapshot(ID_A)
    with pytest.raises(RuntimeError, match="snapshot file missing"):
        host_manager.pause_sandbox(ID_A)
    assert fake_firecracker.killed == []


def test_pause_leaves_a_paused_sandboxs_snapshot_alone(fake_sandboxes_root):
    """Pausing twice must not delete the only copy of a paused sandbox."""
    write_full_snapshot(ID_A)                          # no PID file, so it isn't running
    with pytest.raises(RuntimeError, match="not running"):
        host_manager.pause_sandbox(ID_A)
    assert os.path.getsize(host_manager.mem_path(ID_A)) == FULL_MEM_BYTES


@pytest.mark.parametrize("path_of_missing_file", [
    host_manager.vmstate_path, host_manager.mem_path, host_manager.disk_path,
], ids=["vmstate", "mem", "rootfs"])
def test_resume_raises_when_a_file_is_missing(fake_sandboxes_root, monkeypatch, path_of_missing_file):
    monkeypatch.setattr(host_manager, "start_vm_process", lambda sandbox_id, n: pytest.fail("must not start"))
    write_full_snapshot(ID_A)
    os.remove(path_of_missing_file(ID_A))
    with pytest.raises(FileNotFoundError):
        host_manager.resume_sandbox(ID_A)


def test_resume_kills_firecracker_and_keeps_the_snapshot_if_a_step_fails(fake_sandboxes_root, monkeypatch):
    """The snapshot is the only copy of the sandbox, so a failed resume must leave it for a retry."""
    killed = []
    skip_network_and_cgroup(monkeypatch)
    monkeypatch.setattr(host_manager, "start_vm_process", lambda sandbox_id, n: 4242)
    monkeypatch.setattr(host_manager, "kill_firecracker", killed.append)

    def failing_load(sandbox_id):
        raise RuntimeError("snapshot/load rejected")

    monkeypatch.setattr(host_manager, "restore_vm", failing_load)
    write_full_snapshot(ID_A)
    insert_slot(2, ID_A)

    with pytest.raises(RuntimeError, match="snapshot/load rejected"):
        host_manager.resume_sandbox(ID_A)
    assert killed == [ID_A]
    assert os.path.exists(host_manager.vmstate_path(ID_A))
    assert os.path.exists(host_manager.mem_path(ID_A))


# --- guest disk sync and clock (4b full) ---------------------------------------

class FakeGuestAgent:
    """Stands in for the sandbox manager's HTTP API, remembering every POST and answering with a set status."""

    def __init__(self, status_code=200):
        self.status_code = status_code
        self.posts = []

    def post(self, url, json=None, timeout=None):
        """Records one POST and answers it like httpx would."""
        self.posts.append((url, json))
        return httpx.Response(self.status_code, json={"ok": True}, request=httpx.Request("POST", url))


@pytest.fixture
def fake_guest(monkeypatch):
    """Send the host manager's calls to the sandbox manager (sandbox_manager/) to a fake instead of a real VM."""
    guest = FakeGuestAgent()
    monkeypatch.setattr(host_manager.httpx, "post", guest.post)
    monkeypatch.setattr(host_manager, "sandbox_manager_url", lambda sandbox_id, path: f"http://guest{path}")
    return guest


def test_sync_calls_the_guest_sync_endpoint(fake_guest):
    host_manager.sync_guest_disk(ID_A)
    assert fake_guest.posts == [("http://guest/sync", None)]


def test_sync_fails_the_pause_if_the_guest_cannot_sync(fake_guest):
    """A pause without a flushed disk could corrupt files, so an error must stop it."""
    fake_guest.status_code = 500
    with pytest.raises(httpx.HTTPStatusError):
        host_manager.sync_guest_disk(ID_A)


def test_set_clock_sends_the_hosts_time(fake_guest, monkeypatch):
    monkeypatch.setattr(host_manager.time, "time_ns", lambda: 1_794_960_000_000_000_000)
    host_manager.set_guest_clock(ID_A)
    assert fake_guest.posts == [("http://guest/clock", {"unix_ns": 1_794_960_000_000_000_000})]


@pytest.fixture
def resumable(fake_sandboxes_root, monkeypatch):
    """Make resume_sandbox run without Firecracker, recording the order of its steps."""
    steps = []
    skip_network_and_cgroup(monkeypatch)
    monkeypatch.setattr(host_manager, "start_vm_process", lambda sandbox_id, n: steps.append("start") or 4242)
    monkeypatch.setattr(host_manager, "restore_vm", lambda sandbox_id: steps.append("load"))
    monkeypatch.setattr(host_manager, "wait_for_sandbox_manager", lambda sandbox_id: steps.append("health"))
    write_full_snapshot(ID_A)
    insert_slot(2, ID_A)
    return steps


def test_resume_sets_the_clock_after_the_guest_answers(resumable, fake_guest):
    timings = host_manager.resume_sandbox(ID_A)
    assert resumable == ["start", "load", "health"]
    assert [url for url, body in fake_guest.posts] == ["http://guest/clock"]
    assert timings["clock_set"] is True
    assert "clock_s" in timings


def test_resume_still_succeeds_if_the_clock_cannot_be_set(resumable, fake_guest, monkeypatch):
    """A sandbox manager from before /clock answers 404; the sandbox must still come back."""
    killed = []
    monkeypatch.setattr(host_manager, "kill_firecracker", killed.append)
    fake_guest.status_code = 404
    timings = host_manager.resume_sandbox(ID_A)
    assert timings["clock_set"] is False
    assert killed == []


# --- CPU (cgroup cpu.stat) --------------------------------------------------

@pytest.fixture
def fake_cgroup_root(tmp_path, monkeypatch):
    """Point CGROUP_ROOT at a temporary folder instead of /sys/fs/cgroup/sandboxes."""
    root = str(tmp_path / "cgroup")
    os.makedirs(root)
    monkeypatch.setattr(host_manager, "CGROUP_ROOT", root)
    return root


def write_cpu_stat(sandbox_id, usage_usec):
    """Write a cgroup cpu.stat that includes usage_usec among the other counters."""
    folder = host_manager.cgroup_dir(sandbox_id)
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "cpu.stat"), "w", encoding="utf-8") as stat:
        stat.write(f"user_usec {usage_usec // 2}\n")
        stat.write(f"usage_usec {usage_usec}\n")
        stat.write(f"system_usec {usage_usec // 2}\n")


def test_read_cpu_usage_usec_returns_none_when_the_file_is_missing(fake_cgroup_root):
    """A killed VM has no cgroup, so there is no usage to read."""
    assert host_manager.read_cpu_usage_usec(ID_A) is None


def test_read_cpu_usage_usec_reads_the_counter(fake_cgroup_root):
    """usage_usec is the total CPU time, in microseconds, from cpu.stat."""
    write_cpu_stat(ID_A, 2_500_000)
    assert host_manager.read_cpu_usage_usec(ID_A) == 2_500_000


def test_read_cpu_usage_usec_rejects_a_bad_id(fake_cgroup_root):
    with pytest.raises(ValueError):
        host_manager.read_cpu_usage_usec("../etc")


def test_cpu_percent_from_two_readings():
    """Half a core-second of CPU in one wall second is 50 percent of one core."""
    assert host_manager.cpu_percent(1_000_000, 1_500_000, 1.0) == 50.0


def test_cpu_percent_is_none_without_a_span():
    """The first reading, a zero gap, or a counter that went backwards is not a percent."""
    assert host_manager.cpu_percent(None, 1_000_000, 1.0) is None
    assert host_manager.cpu_percent(1_000_000, None, 1.0) is None
    assert host_manager.cpu_percent(1_000_000, 1_500_000, 0.0) is None
    assert host_manager.cpu_percent(1_500_000, 1_000_000, 1.0) is None
