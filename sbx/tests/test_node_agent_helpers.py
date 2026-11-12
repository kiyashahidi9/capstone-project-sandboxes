import os
import sqlite3
import threading
from contextlib import closing

import pytest

import node_agent


ID_A = "sbx_" + "a" * 32
ID_B = "sbx_" + "b" * 32


# --- names and IDs ----------------------------------------------------------

def test_new_ids_are_valid_and_unique():
    ids = {node_agent.new_sandbox_id() for _ in range(1000)}
    assert len(ids) == 1000
    for sandbox_id in ids:
        node_agent.validate_sandbox_id(sandbox_id)   # does not raise


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
    root = str(tmp_path)          # pytest gives a Path; we use plain strings
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
    with pytest.raises(sqlite3.IntegrityError):        # sandbox_id is UNIQUE
        insert_slot(5, ID_A)


def test_concurrent_claims_never_share_a_slot(fake_sandboxes_root):
    """20 threads claim at the same moment: every one gets a different slot."""
    ids = [f"sbx_{i:032x}" for i in range(20)]
    start = threading.Barrier(len(ids))
    results = {}

    def claim(sandbox_id):
        start.wait()                                   # release all threads at once
        results[sandbox_id] = node_agent.claim_free_slot(sandbox_id)

    threads = [threading.Thread(target=claim, args=(i,)) for i in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results.values()) == list(range(1, 21))   # 20 different slots, no gaps


def test_find_slot(fake_sandboxes_root):
    assert node_agent.find_slot(ID_A) is None          # empty database
    n = node_agent.claim_free_slot(ID_A)
    assert node_agent.find_slot(ID_A) == n
    assert node_agent.find_slot(ID_B) is None


def test_freed_slot_is_reused_under_a_new_id(fake_sandboxes_root):
    """The whole point of the change: slot numbers are reused, IDs are not."""
    n = node_agent.claim_free_slot(ID_A)
    node_agent.release_slot(n, ID_A)
    assert node_agent.claim_free_slot(ID_B) == n
    assert node_agent.find_slot(ID_A) is None          # old ID no longer reaches slot n


def test_release_does_not_free_another_sandboxes_slot(fake_sandboxes_root):
    n = node_agent.claim_free_slot(ID_B)
    node_agent.release_slot(n, ID_A)                   # A doesn't hold it
    assert node_agent.find_slot(ID_B) == n
    node_agent.release_slot(99, ID_A)                  # missing slot: no error


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
    # PID 1 (init) exists but is certainly not this sandbox's Firecracker.
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
    os.mkdir(node_agent.sandbox_dir(ID_A))           # ID_A already exists on this host
    with pytest.raises(FileExistsError):
        node_agent.create_sandbox(ID_A)
    assert os.path.isdir(node_agent.sandbox_dir(ID_A))   # the existing sandbox was not touched


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

    assert events == [("network", n, n)]               # slot still held while network was deleted
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
    node_agent.save_pid(ID_A, 1)                       # PID 1 exists but isn't this sandbox's Firecracker
    assert node_agent.is_sandbox_running(ID_A) is False


def test_is_sandbox_running_rejects_bad_ids():
    with pytest.raises(ValueError):
        node_agent.is_sandbox_running("../etc")


def test_list_host_sandboxes(fake_sandboxes_root):
    assert node_agent.list_host_sandboxes() == []
    os.mkdir(node_agent.sandbox_dir(ID_A))             # has a folder
    node_agent.claim_free_slot(ID_B)                   # holds a slot but its folder is gone (orphan)
    os.mkdir(f"{fake_sandboxes_root}/sbx1")            # old-format leftover: not ours, ignored
    assert node_agent.list_host_sandboxes() == sorted([ID_A, ID_B])
