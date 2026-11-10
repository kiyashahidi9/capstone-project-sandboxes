"""
Fast unit tests for node_agent.py helpers. No VM, no root: run anywhere.

    uv run pytest tests/test_node_agent_helpers.py -v
"""

import os

import pytest

import node_agent


# --- names and IDs ----------------------------------------------------------

def test_id_and_number_round_trip():
    assert node_agent.sandbox_id_for(7) == "sbx7"
    assert node_agent.number_from_id("sbx7") == 7
    assert node_agent.guest_ip_for(7) == "10.201.0.7"


@pytest.mark.parametrize("bad_id", ["", "sbx", "sbx0", "sbx251", "abc3", "sbx3/../..", "../etc"])
def test_number_from_id_rejects_bad_ids(bad_id):
    with pytest.raises(ValueError):
        node_agent.number_from_id(bad_id)


def test_guest_agent_url():
    assert node_agent.guest_agent_url("sbx2", "/health") == "http://10.201.0.2:49983/health"


# --- claim_free_number ------------------------------------------------------

@pytest.fixture
def fake_sandboxes_root(tmp_path, monkeypatch):
    """Point SANDBOXES_ROOT at a temporary folder instead of /var/sbx."""
    root = str(tmp_path)          # pytest gives a Path; we use plain strings
    monkeypatch.setattr(node_agent, "SANDBOXES_ROOT", root)
    return root


def test_claims_lowest_free_number(fake_sandboxes_root):
    os.mkdir(f"{fake_sandboxes_root}/sbx1")
    os.mkdir(f"{fake_sandboxes_root}/sbx3")
    assert node_agent.claim_free_number() == 2
    assert os.path.isdir(f"{fake_sandboxes_root}/sbx2")
    assert node_agent.claim_free_number() == 4


def test_raises_when_all_numbers_taken(fake_sandboxes_root, monkeypatch):
    monkeypatch.setattr(node_agent, "MAX_SANDBOX_NUMBER", 2)
    node_agent.claim_free_number()
    node_agent.claim_free_number()
    with pytest.raises(RuntimeError, match="in use"):
        node_agent.claim_free_number()


# --- pid file and wait_until --------------------------------------------------

def test_pid_round_trip(fake_sandboxes_root):
    os.mkdir(f"{fake_sandboxes_root}/sbx1")
    assert node_agent.load_pid("sbx1") is None
    node_agent.save_pid("sbx1", 4242)
    assert node_agent.load_pid("sbx1") == 4242


def test_stale_pid_is_not_ours():
    # PID 1 (init) exists but is certainly not sbx1's Firecracker.
    assert node_agent.pid_still_belongs_to_sandbox(1, "sbx1") is False


def test_wait_until_times_out():
    with pytest.raises(TimeoutError, match="the thing"):
        node_agent.wait_until(lambda: False, timeout=0.1, interval=0.02, waiting_for="the thing")


# --- create_sandbox cleans up on failure ---------------------------------------

def test_create_sandbox_cleans_up_when_a_step_fails(fake_sandboxes_root, monkeypatch):
    """If any step fails, destroy_sandbox runs and the original error is raised."""
    destroyed = []
    monkeypatch.setattr(node_agent, "ensure_cgroup_parent", lambda: None)
    monkeypatch.setattr(node_agent, "copy_base_image", lambda sandbox_id: None)

    def failing_network(n):
        raise RuntimeError("netns_create.sh failed")

    monkeypatch.setattr(node_agent, "create_network", failing_network)
    monkeypatch.setattr(node_agent, "destroy_sandbox", destroyed.append)

    with pytest.raises(RuntimeError, match="netns_create.sh failed"):
        node_agent.create_sandbox()
    assert destroyed == ["sbx1"]
