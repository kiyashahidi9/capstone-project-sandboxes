import os
import subprocess
import time

import pytest

import host_manager
from host_manager import (
    create_sandbox, destroy_sandbox, exec, sandbox_manager_is_healthy, is_sandbox_running,
    list_host_sandboxes, new_sandbox_id, pause_sandbox, read_file, resume_sandbox, write_file,
)

pytestmark = pytest.mark.skipif(
    os.geteuid() != 0 or not os.path.exists("/dev/kvm"),
    reason="needs root and /dev/kvm (run on the sandbox host with sudo)",
)

# Writes to a temp file then renames it, so a read never sees a half-written number.
COUNTER_COMMAND = (
    "i=0; while true; do echo $i > /root/count.tmp; mv /root/count.tmp /root/count; "
    "i=$((i+1)); sleep 1; done"
)
COUNTER_WAIT = 3                                       # seconds, long enough for the count to rise
CLOCK_PAUSE_WAIT = 10                                  # seconds paused: a clock left unset would be this far behind
CLOCK_TOLERANCE = 2                                    # seconds; `date +%s` rounds down and exec takes a moment


def start_counter(sandbox_id):
    """Start a loop in the sandbox that writes a rising number to /root/count every second."""
    exec(sandbox_id, COUNTER_COMMAND, background=True)


def read_counter(sandbox_id):
    """Return the number the counter loop wrote last."""
    return int(read_file(sandbox_id, "/root/count").strip())


def firecracker_pids(sandbox_id):
    """Return the PIDs of any Firecracker still serving this sandbox (empty if none)."""
    found = subprocess.run(["pgrep", "-f", f"{sandbox_id}/fc.sock"], capture_output=True, text=True)
    return found.stdout.strip()


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


def test_pause_and_resume_keep_a_running_process():
    """A background process keeps counting across a pause and resume (4b)."""
    sbx = create_sandbox()
    try:
        start_counter(sbx.id)
        time.sleep(COUNTER_WAIT)
        count_before_pause = read_counter(sbx.id)

        pause_timings = pause_sandbox(sbx.id)
        assert firecracker_pids(sbx.id) == ""
        assert os.path.exists(host_manager.vmstate_path(sbx.id))
        assert os.path.exists(host_manager.mem_path(sbx.id))

        resumed = resume_sandbox(sbx.id)
        print(f"\npause: {pause_timings}\nresume: {resumed}")
        assert resumed["pid"] != sbx.pid
        assert sandbox_manager_is_healthy(sbx.id)

        time.sleep(COUNTER_WAIT)
        assert read_counter(sbx.id) > count_before_pause
    finally:
        destroy_sandbox(sbx.id)


def guest_clock_seconds(sandbox_id):
    """Return the guest's wall-clock time, in whole seconds since 1970."""
    return int(exec(sandbox_id, "date +%s")["stdout"].strip())


def test_resume_sets_the_guest_clock_to_the_hosts_time():
    """Without /clock the guest would wake up CLOCK_PAUSE_WAIT seconds behind (4b full)."""
    sbx = create_sandbox()
    try:
        pause_sandbox(sbx.id)
        time.sleep(CLOCK_PAUSE_WAIT)
        resumed = resume_sandbox(sbx.id)
        assert resumed["clock_set"] is True
        assert abs(guest_clock_seconds(sbx.id) - time.time()) < CLOCK_TOLERANCE
    finally:
        destroy_sandbox(sbx.id)


def test_destroy_a_paused_sandbox():
    """Destroying a paused sandbox leaves no process, cgroup, slot or folder (4b step 6)."""
    sbx = create_sandbox()
    try:
        pause_sandbox(sbx.id)
    finally:
        destroy_sandbox(sbx.id)
    assert firecracker_pids(sbx.id) == ""
    assert not os.path.exists(host_manager.cgroup_dir(sbx.id))
    assert host_manager.find_slot(sbx.id) is None
    assert not os.path.exists(host_manager.sandbox_dir(sbx.id))


def test_nothing_is_left_behind():
    """
    After the tests above, the host is clean ("Done when" in 1f).

    Runs last (pytest keeps file order), so it checks the cleanup above.
    """
    namespaces = subprocess.run(["ip", "netns", "list"], capture_output=True, text=True).stdout
    leftover_processes = subprocess.run(["pgrep", "firecracker"], capture_output=True, text=True).stdout

    assert namespaces.strip() == ""
    cgroup_root = host_manager.CGROUP_ROOT
    leftover_cgroups = [
        name for name in os.listdir(cgroup_root)
        if os.path.isdir(f"{cgroup_root}/{name}")
    ]

    leftover_files = [
        name for name in os.listdir(host_manager.SANDBOXES_ROOT)
        if not name.startswith("state.db")              # the database (and its -wal/-shm files) stays
    ]
    assert leftover_files == []
    assert host_manager.slot_holders() == []              # every slot was released
    assert leftover_cgroups == []
    assert leftover_processes.strip() == ""
