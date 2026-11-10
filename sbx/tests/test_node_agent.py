"""
Integration test for node_agent.py (Build Plan step 1f).

Boots two real Firecracker VMs, so it only runs on the host, as root:

    sudo .venv/bin/python -m pytest tests/test_node_agent.py -v

Skipped automatically on machines without root or /dev/kvm (laptops, CI).
"""

import os
import subprocess

import pytest

import node_agent
from node_agent import (
    create_sandbox, destroy_sandbox, exec, is_sandbox_running, list_host_sandboxes,
    new_sandbox_id, read_file, write_file,
)

pytestmark = pytest.mark.skipif(
    os.geteuid() != 0 or not os.path.exists("/dev/kvm"),
    reason="needs root and /dev/kvm (run on the sandbox host with sudo)",
)


def test_two_sandboxes_run_commands_and_are_isolated():
    """Two sandboxes run commands, and one cannot see the other's files."""
    a = create_sandbox()
    b = create_sandbox()
    try:
        print(f"\nboot times: {a.id}={a.boot_seconds}s {b.id}={b.boot_seconds}s")
        assert a.id != b.id
        assert a.n != b.n

        assert exec(a.id, "echo hi")["stdout"].strip() == "hi"
        assert exec(b.id, "echo hi")["stdout"].strip() == "hi"

        write_file(a.id, "/root/x.txt", "from a")
        assert read_file(a.id, "/root/x.txt") == "from a"
        assert exec(b.id, "cat /root/x.txt")["exit_code"] != 0   # b can't see a's files
    finally:
        destroy_sandbox(a.id)
        destroy_sandbox(b.id)


def test_slot_is_reused_under_a_new_id():
    """A destroyed sandbox's slot goes to the next sandbox, but its ID never does."""
    first = create_sandbox()
    destroy_sandbox(first.id)
    second = create_sandbox()
    try:
        assert second.n == first.n
        assert second.id != first.id
        with pytest.raises(LookupError):
            exec(first.id, "echo hi")          # old ID can't reach the new sandbox
        assert exec(second.id, "echo hi")["stdout"].strip() == "hi"
    finally:
        destroy_sandbox(second.id)


def test_api_server_helpers():
    """What 1g relies on: pass in an ID, check it's running, find it on the host, survive Ctrl+C."""
    sandbox_id = new_sandbox_id()
    sbx = create_sandbox(sandbox_id)
    try:
        assert sbx.id == sandbox_id
        assert is_sandbox_running(sandbox_id)
        assert sandbox_id in list_host_sandboxes()
        # Firecracker is in its own session, so Ctrl+C on the API server doesn't reach it.
        assert os.getsid(sbx.pid) != os.getsid(0)
    finally:
        destroy_sandbox(sandbox_id)
    assert not is_sandbox_running(sandbox_id)
    assert sandbox_id not in list_host_sandboxes()


def test_nothing_is_left_behind():
    """
    After the tests above, the host is clean ("Done when" in 1f).

    Runs last (pytest keeps file order), so it checks the cleanup above.
    """
    namespaces = subprocess.run(["ip", "netns", "list"], capture_output=True, text=True).stdout
    leftover_processes = subprocess.run(["pgrep", "firecracker"], capture_output=True, text=True).stdout

    assert namespaces.strip() == ""
    cgroup_root = node_agent.CGROUP_ROOT
    leftover_cgroups = [
        name for name in os.listdir(cgroup_root)
        if os.path.isdir(f"{cgroup_root}/{name}")
    ]

    assert os.listdir(node_agent.SANDBOXES_ROOT) in ([], ["slots"])   # the empty slots/ folder may stay
    assert not os.path.isdir(node_agent.slots_dir()) or os.listdir(node_agent.slots_dir()) == []
    assert leftover_cgroups == []
    assert leftover_processes.strip() == ""
