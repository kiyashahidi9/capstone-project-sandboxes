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
from node_agent import create_sandbox, destroy_sandbox, exec, read_file, write_file

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

        assert exec(a.id, "echo hi")["stdout"].strip() == "hi"
        assert exec(b.id, "echo hi")["stdout"].strip() == "hi"

        write_file(a.id, "/root/x.txt", "from a")
        assert read_file(a.id, "/root/x.txt") == "from a"
        assert exec(b.id, "cat /root/x.txt")["exit_code"] != 0   # b can't see a's files
    finally:
        destroy_sandbox(a.id)
        destroy_sandbox(b.id)


def test_nothing_is_left_behind():
    """
    After the test above, the host is clean ("Done when" in 1f).

    Runs second (pytest keeps file order), so it checks the cleanup above.
    """
    namespaces = subprocess.run(["ip", "netns", "list"], capture_output=True, text=True).stdout
    leftover_processes = subprocess.run(["pgrep", "firecracker"], capture_output=True, text=True).stdout

    assert namespaces.strip() == ""
    cgroup_root = node_agent.CGROUP_ROOT
    leftover_cgroups = [
        name for name in os.listdir(cgroup_root)
        if os.path.isdir(f"{cgroup_root}/{name}")
    ]

    assert os.listdir(node_agent.SANDBOXES_ROOT) == []
    assert leftover_cgroups == []
    assert leftover_processes.strip() == ""
