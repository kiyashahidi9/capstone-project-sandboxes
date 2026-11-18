import os
import sqlite3
import threading
from contextlib import closing, nullcontext

import pytest

import node_agent


ID_A = "sbx_" + "a" * 32
ID_B = "sbx_" + "b" * 32


# --- names and IDs ----------------------------------------------------------

def test_new_ids_are_valid_and_unique():
    ids = {node_agent.new_sandbox_id() for _ in range(1000)}
    assert len(ids) == 1000
    for sandbox_id in ids:
        node_agent.validate_sandbox_id(sandbox_id)


def test_names_built_from_slot_number():
    assert node_agent.namespace_for(7) == "sbx7"
    assert node_agent.guest_ip_for(7) == "10.201.0.7"


@pytest.mark.parametrize("bad_id", [
    "", "sbx", "sbx3",                       # old format is no longer accepted
    "sbx_" + "a" * 31, "sbx_" + "A" * 32,    # wrong length, uppercase
    "sbx_" + "a" * 32 + "/..", "../etc", "state.db",
])
def test_validate_rejects_bad_ids(bad_id):
    with pytest.raises(ValueError):
        node_agent.validate_sandbox_id(bad_id)


# --- slots (SQLite) -------------------------------------------------------------

@pytest.fixture
def fake_sandboxes_root(tmp_path, monkeypatch):
    """Point SANDBOXES_ROOT (and so the state database) at a temporary folder instead of /var/sbx."""
    root = str(tmp_path)
    monkeypatch.setattr(node_agent, "SANDBOXES_ROOT", root)
    return root


def insert_slot(n, sandbox_id):
    """Mark slot n as taken by sandbox_id directly in the database (test setup)."""
    with closing(node_agent.open_db()) as conn:
        conn.execute("INSERT INTO slots (n, sandbox_id) VALUES (?, ?)", (n, sandbox_id))


def test_claims_lowest_free_slot(fake_sandboxes_root):
    insert_slot(1, "sbx_" + "1" * 32)
    insert_slot(3, "sbx_" + "3" * 32)
    assert node_agent.claim_free_slot(ID_A) == 2
    assert node_agent.find_slot(ID_A) == 2
    assert node_agent.claim_free_slot(ID_B) == 4


def test_raises_when_all_slots_taken(fake_sandboxes_root, monkeypatch):
    monkeypatch.setattr(node_agent, "MAX_SANDBOX_NUMBER", 2)
    node_agent.claim_free_slot(ID_A)
    node_agent.claim_free_slot(ID_B)
    with pytest.raises(node_agent.NoFreeSlotsError, match="in use"):
        node_agent.claim_free_slot("sbx_" + "c" * 32)


def test_a_sandbox_cannot_hold_two_slots(fake_sandboxes_root):
    node_agent.claim_free_slot(ID_A)
    with pytest.raises(sqlite3.IntegrityError):
        insert_slot(5, ID_A)


def test_concurrent_claims_never_share_a_slot(fake_sandboxes_root):
    """20 threads claim at the same moment: every one gets a different slot."""
    ids = [f"sbx_{i:032x}" for i in range(20)]
    start = threading.Barrier(len(ids))
    results = {}

    def claim(sandbox_id):
        start.wait()
        results[sandbox_id] = node_agent.claim_free_slot(sandbox_id)

    threads = [threading.Thread(target=claim, args=(i,)) for i in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results.values()) == list(range(1, 21))


def test_find_slot(fake_sandboxes_root):
    assert node_agent.find_slot(ID_A) is None
    n = node_agent.claim_free_slot(ID_A)
    assert node_agent.find_slot(ID_A) == n
    assert node_agent.find_slot(ID_B) is None


def test_freed_slot_is_reused_under_a_new_id(fake_sandboxes_root):
    """The whole point of the change: slot numbers are reused, IDs are not."""
    n = node_agent.claim_free_slot(ID_A)
    node_agent.release_slot(n, ID_A)
    assert node_agent.claim_free_slot(ID_B) == n
    assert node_agent.find_slot(ID_A) is None


def test_release_does_not_free_another_sandboxes_slot(fake_sandboxes_root):
    n = node_agent.claim_free_slot(ID_B)
    node_agent.release_slot(n, ID_A)
    assert node_agent.find_slot(ID_B) == n
    node_agent.release_slot(99, ID_A)


def test_require_slot_raises_lookup_error_for_unknown_sandbox(fake_sandboxes_root):
    with pytest.raises(LookupError):
        node_agent.require_slot(ID_A)
    with pytest.raises(ValueError):
        node_agent.require_slot("../etc")


def test_guest_agent_url(fake_sandboxes_root):
    insert_slot(2, ID_A)
    assert node_agent.guest_agent_url(ID_A, "/health") == "http://10.201.0.2:49983/health"


# --- pid file and wait_until --------------------------------------------------

def test_pid_round_trip(fake_sandboxes_root):
    os.mkdir(node_agent.sandbox_dir(ID_A))
    assert node_agent.load_pid(ID_A) is None
    node_agent.save_pid(ID_A, 4242)
    assert node_agent.load_pid(ID_A) == 4242


def test_stale_pid_is_not_ours():
    assert node_agent.pid_still_belongs_to_sandbox(1, ID_A) is False


def test_wait_until_times_out():
    with pytest.raises(TimeoutError, match="the thing"):
        node_agent.wait_until(lambda: False, timeout=0.1, interval=0.02, waiting_for="the thing")


# --- create_sandbox / destroy_sandbox ------------------------------------------

def test_create_sandbox_cleans_up_when_a_step_fails(fake_sandboxes_root, monkeypatch):
    """If any step fails, destroy_sandbox runs and the original error is raised."""
    destroyed = []
    monkeypatch.setattr(node_agent, "new_sandbox_id", lambda: ID_A)
    monkeypatch.setattr(node_agent, "ensure_cgroup_parent", lambda: None)
    monkeypatch.setattr(node_agent, "copy_base_image", lambda sandbox_id: None)

    def failing_network(n):
        raise RuntimeError("netns_create.sh failed")

    monkeypatch.setattr(node_agent, "create_network", failing_network)
    monkeypatch.setattr(node_agent, "destroy_sandbox", destroyed.append)

    with pytest.raises(RuntimeError, match="netns_create.sh failed"):
        node_agent.create_sandbox()
    assert destroyed == [ID_A]


def test_create_sandbox_uses_the_id_it_is_given(fake_sandboxes_root, monkeypatch):
    """1g makes the ID first (to save its DB row), then passes it in."""
    destroyed = []
    monkeypatch.setattr(node_agent, "ensure_cgroup_parent", lambda: None)
    monkeypatch.setattr(node_agent, "copy_base_image", lambda sandbox_id: None)
    def failing_network(n):
        raise RuntimeError("stop here")

    monkeypatch.setattr(node_agent, "create_network", failing_network)
    monkeypatch.setattr(node_agent, "destroy_sandbox", destroyed.append)

    with pytest.raises(RuntimeError, match="stop here"):
        node_agent.create_sandbox(ID_B)
    assert destroyed == [ID_B]


def test_create_sandbox_rejects_bad_or_reused_ids(fake_sandboxes_root, monkeypatch):
    monkeypatch.setattr(node_agent, "ensure_cgroup_parent", lambda: None)
    with pytest.raises(ValueError):
        node_agent.create_sandbox("../etc")
    os.mkdir(node_agent.sandbox_dir(ID_A))
    with pytest.raises(FileExistsError):
        node_agent.create_sandbox(ID_A)
    assert os.path.isdir(node_agent.sandbox_dir(ID_A))


def test_destroy_frees_slot_only_after_network_is_deleted(fake_sandboxes_root, monkeypatch):
    """A new sandbox must not get the slot while the old one's veth still exists."""
    events = []
    monkeypatch.setattr(node_agent, "kill_firecracker", lambda sandbox_id: None)
    monkeypatch.setattr(node_agent, "remove_cgroup", lambda sandbox_id: None)
    monkeypatch.setattr(
        node_agent, "delete_network",
        lambda n: events.append(("network", n, node_agent.find_slot(ID_A))),
    )
    os.mkdir(node_agent.sandbox_dir(ID_A))
    n = node_agent.claim_free_slot(ID_A)

    node_agent.destroy_sandbox(ID_A)

    assert events == [("network", n, n)]
    assert node_agent.find_slot(ID_A) is None
    assert not os.path.exists(node_agent.sandbox_dir(ID_A))


def test_destroy_keeps_the_slot_if_network_delete_fails(fake_sandboxes_root, monkeypatch):
    """If the network can't be removed, the slot must stay taken so nobody collides with it."""
    monkeypatch.setattr(node_agent, "kill_firecracker", lambda sandbox_id: None)
    monkeypatch.setattr(node_agent, "remove_cgroup", lambda sandbox_id: None)

    def failing_delete(n):
        raise RuntimeError("netns_delete.sh failed")

    monkeypatch.setattr(node_agent, "delete_network", failing_delete)
    os.mkdir(node_agent.sandbox_dir(ID_A))
    n = node_agent.claim_free_slot(ID_A)

    with pytest.raises(RuntimeError):
        node_agent.destroy_sandbox(ID_A)
    assert node_agent.find_slot(ID_A) == n


def test_destroy_without_a_slot_skips_network(fake_sandboxes_root, monkeypatch):
    """create can fail before claiming a slot; destroy must still clean the folder."""
    monkeypatch.setattr(node_agent, "kill_firecracker", lambda sandbox_id: None)
    monkeypatch.setattr(node_agent, "remove_cgroup", lambda sandbox_id: None)
    monkeypatch.setattr(node_agent, "delete_network", lambda n: pytest.fail("no slot, no network"))
    os.mkdir(node_agent.sandbox_dir(ID_A))
    node_agent.destroy_sandbox(ID_A)
    assert not os.path.exists(node_agent.sandbox_dir(ID_A))


# --- for the API server (1g) ---------------------------------------------------

def test_is_sandbox_running_without_a_pid(fake_sandboxes_root):
    os.mkdir(node_agent.sandbox_dir(ID_A))
    assert node_agent.is_sandbox_running(ID_A) is False
    node_agent.save_pid(ID_A, 1)
    assert node_agent.is_sandbox_running(ID_A) is False


def test_is_sandbox_running_rejects_bad_ids():
    with pytest.raises(ValueError):
        node_agent.is_sandbox_running("../etc")


def test_list_host_sandboxes(fake_sandboxes_root):
    assert node_agent.list_host_sandboxes() == []
    os.mkdir(node_agent.sandbox_dir(ID_A))
    node_agent.claim_free_slot(ID_B)
    os.mkdir(f"{fake_sandboxes_root}/sbx1")
    assert node_agent.list_host_sandboxes() == sorted([ID_A, ID_B])


# --- pause_sandbox / resume_sandbox (4b) ----------------------------------------

FULL_MEM_BYTES = node_agent.MEMORY_MIB * node_agent.BYTES_PER_MIB


def write_file_of_size(path, size):
    """Create a file of `size` bytes, making its folder if needed (test setup)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.truncate(size)


def write_full_snapshot(sandbox_id):
    """Put a complete-looking snapshot and disk in place for sandbox_id (test setup)."""
    write_file_of_size(node_agent.vmstate_path(sandbox_id), 1)
    write_file_of_size(node_agent.mem_path(sandbox_id), FULL_MEM_BYTES)
    write_file_of_size(node_agent.disk_path(sandbox_id), 1)


class FakeFirecracker:
    """Stands in for Firecracker during pause tests, remembering every call it gets."""

    def __init__(self):
        self.calls = []
        self.killed = []
        self.mem_bytes_to_write = None

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
    monkeypatch.setattr(node_agent, "require_running", lambda sandbox_id: None)
    monkeypatch.setattr(node_agent, "sync_guest_disk", lambda sandbox_id: None)
    monkeypatch.setattr(node_agent, "firecracker_client", lambda sandbox_id: nullcontext())
    monkeypatch.setattr(node_agent, "firecracker_put", fake.put)
    monkeypatch.setattr(node_agent, "firecracker_patch", fake.patch)
    monkeypatch.setattr(node_agent, "kill_firecracker", fake.killed.append)
    return fake


def test_snapshot_paths(fake_sandboxes_root):
    assert node_agent.snapshot_dir(ID_A) == f"{fake_sandboxes_root}/{ID_A}/snap"
    assert node_agent.vmstate_path(ID_A) == f"{fake_sandboxes_root}/{ID_A}/snap/vmstate"
    assert node_agent.mem_path(ID_A) == f"{fake_sandboxes_root}/{ID_A}/snap/mem"


def test_pause_freezes_then_snapshots_then_kills(fake_firecracker):
    fake_firecracker.mem_bytes_to_write = FULL_MEM_BYTES
    timings = node_agent.pause_sandbox(ID_A)
    assert fake_firecracker.calls == [("PATCH", "/vm", "Paused"), ("PUT", "/snapshot/create")]
    assert fake_firecracker.killed == [ID_A]
    assert set(timings) == {"sync_s", "freeze_s", "snapshot_s", "kill_s"}


def test_pause_keeps_the_vm_running_if_no_snapshot_was_written(fake_firecracker):
    """No snapshot files: unfreeze the VM and never kill it, or the sandbox is lost."""
    with pytest.raises(RuntimeError, match="snapshot file missing"):
        node_agent.pause_sandbox(ID_A)
    assert fake_firecracker.calls[-1] == ("PATCH", "/vm", "Resumed")
    assert fake_firecracker.killed == []


def test_pause_keeps_the_vm_running_if_mem_is_too_small(fake_firecracker):
    """A cut-short mem file (the 4a gotcha) counts as a failed snapshot."""
    fake_firecracker.mem_bytes_to_write = FULL_MEM_BYTES - 1
    with pytest.raises(RuntimeError, match="mem file is"):
        node_agent.pause_sandbox(ID_A)
    assert fake_firecracker.calls[-1] == ("PATCH", "/vm", "Resumed")
    assert fake_firecracker.killed == []


def test_pause_never_passes_on_files_from_an_earlier_pause(fake_firecracker):
    """Old snapshot files are cleared first, so a pause that writes nothing still fails."""
    write_full_snapshot(ID_A)
    with pytest.raises(RuntimeError, match="snapshot file missing"):
        node_agent.pause_sandbox(ID_A)
    assert fake_firecracker.killed == []


def test_pause_leaves_a_paused_sandboxs_snapshot_alone(fake_sandboxes_root):
    """Pausing twice must not delete the only copy of a paused sandbox."""
    write_full_snapshot(ID_A)
    with pytest.raises(RuntimeError, match="not running"):
        node_agent.pause_sandbox(ID_A)
    assert os.path.getsize(node_agent.mem_path(ID_A)) == FULL_MEM_BYTES


@pytest.mark.parametrize("path_of_missing_file", [
    node_agent.vmstate_path, node_agent.mem_path, node_agent.disk_path,
], ids=["vmstate", "mem", "rootfs"])
def test_resume_raises_when_a_file_is_missing(fake_sandboxes_root, monkeypatch, path_of_missing_file):
    monkeypatch.setattr(node_agent, "start_vm_process", lambda sandbox_id, n: pytest.fail("must not start"))
    write_full_snapshot(ID_A)
    os.remove(path_of_missing_file(ID_A))
    with pytest.raises(FileNotFoundError):
        node_agent.resume_sandbox(ID_A)


def test_resume_kills_firecracker_and_keeps_the_snapshot_if_a_step_fails(fake_sandboxes_root, monkeypatch):
    """The snapshot is the only copy of the sandbox, so a failed resume must leave it for a retry."""
    killed = []
    monkeypatch.setattr(node_agent, "start_vm_process", lambda sandbox_id, n: 4242)
    monkeypatch.setattr(node_agent, "kill_firecracker", killed.append)

    def failing_load(sandbox_id):
        raise RuntimeError("snapshot/load rejected")

    monkeypatch.setattr(node_agent, "restore_vm", failing_load)
    write_full_snapshot(ID_A)
    insert_slot(2, ID_A)

    with pytest.raises(RuntimeError, match="snapshot/load rejected"):
        node_agent.resume_sandbox(ID_A)
    assert killed == [ID_A]
    assert os.path.exists(node_agent.vmstate_path(ID_A))
    assert os.path.exists(node_agent.mem_path(ID_A))


# --- CPU (cgroup cpu.stat) --------------------------------------------------

@pytest.fixture
def fake_cgroup_root(tmp_path, monkeypatch):
    """Point CGROUP_ROOT at a temporary folder instead of /sys/fs/cgroup/sandboxes."""
    root = str(tmp_path / "cgroup")
    os.makedirs(root)
    monkeypatch.setattr(node_agent, "CGROUP_ROOT", root)
    return root


def write_cpu_stat(sandbox_id, usage_usec):
    """Write a cgroup cpu.stat that includes usage_usec among the other counters."""
    folder = node_agent.cgroup_dir(sandbox_id)
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "cpu.stat"), "w", encoding="utf-8") as stat:
        stat.write(f"user_usec {usage_usec // 2}\n")
        stat.write(f"usage_usec {usage_usec}\n")
        stat.write(f"system_usec {usage_usec // 2}\n")


def test_read_cpu_usage_usec_returns_none_when_the_file_is_missing(fake_cgroup_root):
    """A killed VM has no cgroup, so there is no usage to read."""
    assert node_agent.read_cpu_usage_usec(ID_A) is None


def test_read_cpu_usage_usec_reads_the_counter(fake_cgroup_root):
    """usage_usec is the total CPU time, in microseconds, from cpu.stat."""
    write_cpu_stat(ID_A, 2_500_000)
    assert node_agent.read_cpu_usage_usec(ID_A) == 2_500_000


def test_read_cpu_usage_usec_rejects_a_bad_id(fake_cgroup_root):
    with pytest.raises(ValueError):
        node_agent.read_cpu_usage_usec("../etc")


def test_cpu_percent_from_two_readings():
    """Half a core-second of CPU in one wall second is 50 percent of one core."""
    assert node_agent.cpu_percent(1_000_000, 1_500_000, 1.0) == 50.0


def test_cpu_percent_is_none_without_a_span():
    """The first reading, a zero gap, or a counter that went backwards is not a percent."""
    assert node_agent.cpu_percent(None, 1_000_000, 1.0) is None
    assert node_agent.cpu_percent(1_000_000, None, 1.0) is None
    assert node_agent.cpu_percent(1_000_000, 1_500_000, 0.0) is None
    assert node_agent.cpu_percent(1_500_000, 1_000_000, 1.0) is None
