"""
node_agent.py — control Firecracker sandboxes from Python (Build Plan step 1f).

This module does in code what steps 1c–1e did by hand: copy a disk, create a
network namespace, start Firecracker, configure and boot the VM, and talk to
the guest agent running inside it. There is no web server here; the API
server in step 1g imports these functions.

Public functions (everything else in this file is a helper for these):

    create_sandbox(sandbox_id=None)           -> Sandbox
    destroy_sandbox(sandbox_id)
    exec(sandbox_id, cmd, cwd, background, timeout) -> dict
    read_file(sandbox_id, path)               -> str
    write_file(sandbox_id, path, content)

    For the API server (1g):
    new_sandbox_id()                          -> str
    is_sandbox_running(sandbox_id)            -> bool
    list_host_sandboxes()                     -> list[str]

Errors the API server maps to HTTP codes:

    ValueError     malformed sandbox ID                    -> 404
    LookupError    no such sandbox on this host            -> 404
    httpx.HTTPError  the guest agent didn't answer         -> 409 / 502

Must run as root (it creates namespaces, cgroups and Firecracker processes).

Each sandbox has two identifiers (see README section 1):

    id   "sbx_<32 hex>"  its name: random, permanent, never reused
    n    1..250          its network slot: reused after the sandbox is destroyed

Layout on the host for sandbox <id> holding slot N:

    /var/sbx/<id>/rootfs.ext4           this sandbox's own copy of the disk
    /var/sbx/<id>/fc.sock               Firecracker's API socket
    /var/sbx/<id>/firecracker.pid       Firecracker's process ID
    /var/sbx/<id>/console.log           the VM's serial console output
    /sys/fs/cgroup/sandboxes/<id>/      cgroup holding the Firecracker process
    /var/sbx/slots/<N> -> <id>          symlink: "slot N belongs to <id>"
    network namespace "sbx<N>"          reached from the host at 10.201.0.<N>

All paths in this file are plain strings built with f-strings.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import time
import uuid
from typing import Callable

import httpx
from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Configuration
#
# Every path and number the node agent depends on is defined here, so nothing
# is hidden inside a function. Paths can be overridden with environment
# variables for testing.
# ---------------------------------------------------------------------------

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))   # folder holding this file
SCRIPTS_DIR = f"{REPO_ROOT}/scripts"


def _default_fc_dir() -> str:
    """Return the real user's ~/fc folder (holds vmlinux and rootfs.ext4), even under sudo."""
    if "SBX_FC_DIR" in os.environ:
        return os.environ["SBX_FC_DIR"]
    real_user = os.environ.get("SUDO_USER", "")
    real_home = os.path.expanduser(f"~{real_user}")      # "~alice" -> "/home/alice"
    return f"{real_home}/fc"


FC_DIR = _default_fc_dir()
KERNEL_PATH = f"{FC_DIR}/vmlinux"
BASE_IMAGE_PATH = f"{FC_DIR}/rootfs.ext4"

SANDBOXES_ROOT = os.environ.get("SBX_ROOT", "/var/sbx")
CGROUP_ROOT = os.environ.get("SBX_CGROUP_ROOT", "/sys/fs/cgroup/sandboxes")

MIN_SANDBOX_NUMBER = 1
MAX_SANDBOX_NUMBER = 250          # 10.201.0.<N> must be a valid last octet

# VM settings (the same values we sent by hand with curl in step 1c)
BOOT_ARGS = "console=ttyS0 reboot=k panic=1 pci=off"
VCPU_COUNT = 2
MEMORY_MIB = 1024
GUEST_MAC = "06:00:AC:10:00:02"   # makes the test image use 172.16.0.2
TAP_DEVICE = "tap0"               # same name in every namespace (see 1d)

GUEST_AGENT_PORT = 49983          # same port as E2B's envd (see 1e)

# How long to wait for things (seconds)
SOCKET_WAIT_TIMEOUT = 5.0
SOCKET_POLL_INTERVAL = 0.05
HEALTH_WAIT_TIMEOUT = 30.0
HEALTH_POLL_INTERVAL = 0.2
PROCESS_EXIT_TIMEOUT = 5.0
EXEC_HTTP_MARGIN = 30.0           # extra HTTP time on top of a command's own timeout


# ---------------------------------------------------------------------------
# The Sandbox record
# ---------------------------------------------------------------------------

class Sandbox(BaseModel):
    """
    What create_sandbox() returns: everything needed to find and use a sandbox.

    id            "sbx_3f9c...", permanent and never reused
    n             3, the network slot (decides its namespace and IP)
    ip            "10.201.0.3", the address the host uses to reach the VM
    pid           Firecracker's process ID on the host
    boot_seconds  time from starting Firecracker to the guest agent answering
                  /health (write this in NOTES.md)
    """

    id: str
    n: int
    ip: str
    pid: int
    boot_seconds: float


# ---------------------------------------------------------------------------
# Names and paths
#
# Small functions that turn a sandbox number or ID into a name, path or IP.
# Keeping them in one place means the naming scheme is written down only once.
# ---------------------------------------------------------------------------

SANDBOX_ID_PATTERN = re.compile(r"sbx_[0-9a-f]{32}")


def new_sandbox_id() -> str:
    """Make a new random sandbox ID, e.g. "sbx_3f9c1a7e..."; never reused."""
    return f"sbx_{uuid.uuid4().hex}"


def validate_sandbox_id(sandbox_id: str) -> None:
    """Raise ValueError unless this is one of our IDs, so input like "../etc" never reaches a path we delete."""
    if not SANDBOX_ID_PATTERN.fullmatch(sandbox_id):
        raise ValueError(f"not a sandbox ID: {sandbox_id!r}")


def namespace_for(n: int) -> str:
    """Return the network namespace name for slot n, e.g. 3 -> "sbx3" (netns_create.sh uses the same name)."""
    return f"sbx{n}"


def guest_ip_for(n: int) -> str:
    """Return the address the host uses to reach the sandbox in slot n, e.g. 10.201.0.3."""
    return f"10.201.0.{n}"


def slots_dir() -> str:
    """Return the folder of slot symlinks (a function, not a constant, so tests can move SANDBOXES_ROOT)."""
    return f"{SANDBOXES_ROOT}/slots"


def slot_path(n: int) -> str:
    """Return the symlink that records who holds slot n, e.g. /var/sbx/slots/3."""
    return f"{slots_dir()}/{n}"


def sandbox_dir(sandbox_id: str) -> str:
    """Return the sandbox's folder, e.g. /var/sbx/sbx_3f9c..."""
    return f"{SANDBOXES_ROOT}/{sandbox_id}"


def disk_path(sandbox_id: str) -> str:
    """Return the path of the sandbox's own disk image."""
    return f"{sandbox_dir(sandbox_id)}/rootfs.ext4"


def socket_path(sandbox_id: str) -> str:
    """Return the path of the sandbox's Firecracker API socket."""
    return f"{sandbox_dir(sandbox_id)}/fc.sock"


def pid_file_path(sandbox_id: str) -> str:
    """Return the path of the file that stores the sandbox's Firecracker PID."""
    return f"{sandbox_dir(sandbox_id)}/firecracker.pid"


def console_log_path(sandbox_id: str) -> str:
    """Return the path of the file that captures the VM's console output."""
    return f"{sandbox_dir(sandbox_id)}/console.log"


def cgroup_dir(sandbox_id: str) -> str:
    """Return the sandbox's cgroup folder, e.g. /sys/fs/cgroup/sandboxes/sbx_3f9c..."""
    return f"{CGROUP_ROOT}/{sandbox_id}"


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def wait_until(condition: Callable[[], bool], *, timeout: float,
               interval: float, waiting_for: str) -> None:
    """Poll condition() every `interval` seconds until True; raise TimeoutError after `timeout` seconds."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(interval)
    raise TimeoutError(f"gave up after {timeout}s waiting for {waiting_for}")


def run_script(script_name: str, *args: str) -> None:
    """Run a script from scripts/ on the host and wait; raise RuntimeError with its output if it fails."""
    command = [f"{SCRIPTS_DIR}/{script_name}", *args]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"{script_name} {' '.join(args)} failed "
            f"(exit {result.returncode}):\n{result.stderr or result.stdout}"
        )


def write_text_file(path: str, text: str) -> None:
    """Write text to a file, replacing its contents."""
    with open(path, "w") as f:
        f.write(text)


# ---------------------------------------------------------------------------
# Network slots
#
# The symlink slots/<n> -> <id> is the only record on the host of which
# sandbox holds which slot, so there is no second copy to drift out of sync.
# ---------------------------------------------------------------------------

def claim_free_slot(sandbox_id: str) -> int:
    """Reserve the lowest free slot by creating slots/<n> -> sandbox_id, and return n."""
    os.makedirs(slots_dir(), exist_ok=True)
    for n in range(MIN_SANDBOX_NUMBER, MAX_SANDBOX_NUMBER + 1):
        try:
            os.symlink(sandbox_id, slot_path(n))   # fails if taken; check + create in one step, so no two callers get the same n
            return n
        except FileExistsError:
            continue
    raise RuntimeError(f"all {MAX_SANDBOX_NUMBER} sandbox slots are in use")


def find_slot(sandbox_id: str) -> int | None:
    """Return the slot this sandbox holds, or None if it holds none."""
    if not os.path.isdir(slots_dir()):
        return None
    for name in os.listdir(slots_dir()):
        try:
            if os.readlink(f"{slots_dir()}/{name}") == sandbox_id:
                return int(name)
        except (FileNotFoundError, OSError, ValueError):
            continue                               # released mid-scan, or not one of our symlinks
    return None


def release_slot(n: int, sandbox_id: str) -> None:
    """Free slot n, but only if this sandbox still holds it."""
    try:
        if os.readlink(slot_path(n)) == sandbox_id:
            os.remove(slot_path(n))
    except FileNotFoundError:
        pass


def require_slot(sandbox_id: str) -> int:
    """Return the sandbox's slot; raise LookupError if the sandbox doesn't exist (1g turns this into a 404)."""
    validate_sandbox_id(sandbox_id)
    n = find_slot(sandbox_id)
    if n is None:
        raise LookupError(f"no such sandbox: {sandbox_id}")
    return n


# ---------------------------------------------------------------------------
# create_sandbox, steps 3–5: disk, network, cgroup
# ---------------------------------------------------------------------------

def copy_base_image(sandbox_id: str) -> None:
    """Copy the base disk image into the sandbox's folder (sparse: empty space uses no disk)."""
    subprocess.run(
        ["cp", "--sparse=always", BASE_IMAGE_PATH, disk_path(sandbox_id)],
        check=True,
    )


def create_network(n: int) -> None:
    """Create the sandbox's network namespace, tap0, veth pair, NAT rules and host route."""
    run_script("netns_create.sh", str(n))


def ensure_cgroup_parent() -> None:
    """Create the parent cgroup folder for all sandboxes, if it doesn't exist yet."""
    os.makedirs(CGROUP_ROOT, exist_ok=True)


def create_cgroup(sandbox_id: str) -> None:
    """Create the sandbox's own cgroup folder."""
    os.mkdir(cgroup_dir(sandbox_id))


# ---------------------------------------------------------------------------
# create_sandbox, steps 6–8: start Firecracker
# ---------------------------------------------------------------------------

def start_firecracker(sandbox_id: str, n: int) -> int:
    """Start Firecracker in the background inside the sandbox's namespace and return its PID."""
    command = [
        "ip", "netns", "exec", namespace_for(n),
        "firecracker", "--api-sock", socket_path(sandbox_id),
    ]
    with open(console_log_path(sandbox_id), "w") as console_log:
        process = subprocess.Popen(
            command, stdout=console_log, stderr=subprocess.STDOUT,
            start_new_session=True,   # own session: Ctrl+C on the API server doesn't kill running VMs (1g restart test)
        )
    return process.pid


def move_into_cgroup(sandbox_id: str, pid: int) -> None:
    """Put the Firecracker process into the sandbox's cgroup."""
    write_text_file(f"{cgroup_dir(sandbox_id)}/cgroup.procs", str(pid))


def save_pid(sandbox_id: str, pid: int) -> None:
    """Save Firecracker's PID to a file so destroy_sandbox can find it later."""
    write_text_file(pid_file_path(sandbox_id), str(pid))


def wait_for_socket(sandbox_id: str) -> None:
    """Wait (up to 5 s) until Firecracker's API socket file exists."""
    wait_until(
        lambda: os.path.exists(socket_path(sandbox_id)),
        timeout=SOCKET_WAIT_TIMEOUT,
        interval=SOCKET_POLL_INTERVAL,
        waiting_for=f"Firecracker socket for {sandbox_id}",
    )


# ---------------------------------------------------------------------------
# create_sandbox, step 9: configure and boot the VM through Firecracker's API
#
# Each function below sends one of the curl calls from step 1c.
# ---------------------------------------------------------------------------

def firecracker_client(sandbox_id: str) -> httpx.Client:
    """Return an HTTP client that talks to Firecracker through its socket file."""
    transport = httpx.HTTPTransport(uds=socket_path(sandbox_id))
    return httpx.Client(transport=transport, base_url="http://localhost")


def firecracker_put(client: httpx.Client, path: str, body: dict) -> None:
    """Send one PUT to Firecracker; raise RuntimeError with its error message if rejected."""
    response = client.put(path, json=body)
    if response.is_error:
        raise RuntimeError(
            f"Firecracker rejected PUT {path} ({response.status_code}): {response.text}"
        )


def set_boot_source(client: httpx.Client) -> None:
    """Tell Firecracker which kernel to boot and with which options."""
    firecracker_put(client, "/boot-source", {
        "kernel_image_path": KERNEL_PATH,
        "boot_args": BOOT_ARGS,
    })


def attach_root_drive(client: httpx.Client, sandbox_id: str) -> None:
    """Attach the sandbox's disk image as the VM's writable root drive."""
    firecracker_put(client, "/drives/rootfs", {
        "drive_id": "rootfs",
        "path_on_host": disk_path(sandbox_id),
        "is_root_device": True,
        "is_read_only": False,
    })


def set_machine_config(client: httpx.Client) -> None:
    """Set the VM's number of vCPUs and amount of memory."""
    firecracker_put(client, "/machine-config", {
        "vcpu_count": VCPU_COUNT,
        "mem_size_mib": MEMORY_MIB,
    })


def attach_network(client: httpx.Client) -> None:
    """Connect the VM's network card to tap0 in its namespace."""
    firecracker_put(client, "/network-interfaces/eth0", {
        "iface_id": "eth0",
        "guest_mac": GUEST_MAC,
        "host_dev_name": TAP_DEVICE,
    })


def start_instance(client: httpx.Client) -> None:
    """Boot the VM (only works after the four setup calls)."""
    firecracker_put(client, "/actions", {"action_type": "InstanceStart"})


def configure_and_boot_vm(sandbox_id: str) -> None:
    """Send Firecracker the four setup calls, then boot the VM."""
    with firecracker_client(sandbox_id) as client:
        set_boot_source(client)
        attach_root_drive(client, sandbox_id)
        set_machine_config(client)
        attach_network(client)
        start_instance(client)


# ---------------------------------------------------------------------------
# create_sandbox, step 10: wait for the guest agent
# ---------------------------------------------------------------------------

def guest_agent_url(sandbox_id: str, path: str) -> str:
    """Return the full URL of a guest agent endpoint, e.g. http://10.201.0.3:49983/exec."""
    ip = guest_ip_for(require_slot(sandbox_id))
    return f"http://{ip}:{GUEST_AGENT_PORT}{path}"


def guest_agent_is_healthy(sandbox_id: str) -> bool:
    """Return True if the guest agent answers /health; a connection error means not yet."""
    try:
        response = httpx.get(guest_agent_url(sandbox_id, "/health"), timeout=1.0)
        return response.status_code == 200 and response.json().get("ok") is True
    except (httpx.HTTPError, ValueError):
        return False


def wait_for_guest_agent(sandbox_id: str) -> None:
    """Wait (up to 30 s) until the guest agent inside the VM answers /health."""
    wait_until(
        lambda: guest_agent_is_healthy(sandbox_id),
        timeout=HEALTH_WAIT_TIMEOUT,
        interval=HEALTH_POLL_INTERVAL,
        waiting_for=f"guest agent in {sandbox_id} to answer /health",
    )


# ---------------------------------------------------------------------------
# create_sandbox: the whole sequence
# ---------------------------------------------------------------------------

def create_sandbox(sandbox_id: str | None = None) -> Sandbox:
    """Create and boot a new sandbox; if any step fails, clean up and re-raise the error.

    The API server (1g) passes in an ID from new_sandbox_id() so it can save the
    row as 'starting' before booting. Called with no ID, a new one is made here.
    """
    if sandbox_id is None:
        sandbox_id = new_sandbox_id()
    validate_sandbox_id(sandbox_id)
    ensure_cgroup_parent()
    os.makedirs(sandbox_dir(sandbox_id))   # raises FileExistsError if this ID was already used

    try:
        n = claim_free_slot(sandbox_id)    # inside try, so "all slots in use" still removes the folder
        copy_base_image(sandbox_id)
        create_network(n)
        create_cgroup(sandbox_id)

        boot_started = time.monotonic()
        pid = start_firecracker(sandbox_id, n)
        save_pid(sandbox_id, pid)          # saved first, so cleanup can kill it
        move_into_cgroup(sandbox_id, pid)
        wait_for_socket(sandbox_id)
        configure_and_boot_vm(sandbox_id)
        wait_for_guest_agent(sandbox_id)
        boot_seconds = time.monotonic() - boot_started
    except BaseException:
        destroy_sandbox(sandbox_id)
        raise

    return Sandbox(
        id=sandbox_id, n=n, ip=guest_ip_for(n), pid=pid,
        boot_seconds=round(boot_seconds, 3),
    )


# ---------------------------------------------------------------------------
# destroy_sandbox: helpers
#
# Every teardown step checks whether its thing exists before removing it.
# That makes destroy_sandbox() safe to call on a half-created sandbox (when
# create_sandbox fails partway) and safe to call twice.
# ---------------------------------------------------------------------------

def load_pid(sandbox_id: str) -> int | None:
    """Return the saved Firecracker PID, or None if there isn't one."""
    try:
        with open(pid_file_path(sandbox_id)) as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return None


def pid_still_belongs_to_sandbox(pid: int, sandbox_id: str) -> bool:
    """Check the saved PID is still this sandbox's Firecracker, not a process that reused the number."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmdline = f.read()
    except FileNotFoundError:
        return False
    return socket_path(sandbox_id).encode() in cmdline


def wait_for_exit(pid: int) -> None:
    """Wait until a process has fully exited (and collect it if we started it)."""
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        wait_until(
            lambda: not os.path.exists(f"/proc/{pid}"),
            timeout=PROCESS_EXIT_TIMEOUT,
            interval=0.05,
            waiting_for=f"process {pid} to exit",
        )


def kill_firecracker(sandbox_id: str) -> None:
    """Kill the sandbox's Firecracker process (stopping the VM) and wait for it to exit."""
    pid = load_pid(sandbox_id)
    if pid is None or not pid_still_belongs_to_sandbox(pid, sandbox_id):
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    wait_for_exit(pid)


def remove_cgroup(sandbox_id: str) -> None:
    """Remove the sandbox's cgroup folder (only works once Firecracker is gone)."""
    if os.path.exists(cgroup_dir(sandbox_id)):
        os.rmdir(cgroup_dir(sandbox_id))


def delete_network(n: int) -> None:
    """Delete the sandbox's namespace, veth pair, host route and connection-tracking entries.

    Always runs the script (it skips anything already gone), so a half-built
    network, e.g. a host route left without its namespace, is cleaned up too
    before the slot is handed to the next sandbox.
    """
    run_script("netns_delete.sh", str(n))


def remove_sandbox_dir(sandbox_id: str) -> None:
    """Delete the sandbox's folder and everything in it."""
    if os.path.exists(sandbox_dir(sandbox_id)):
        shutil.rmtree(sandbox_dir(sandbox_id))


# ---------------------------------------------------------------------------
# destroy_sandbox: the whole sequence
# ---------------------------------------------------------------------------

def destroy_sandbox(sandbox_id: str) -> None:
    """Stop the sandbox and remove everything it created; safe on a half-created sandbox."""
    # Order matters: the process must be gone before its cgroup can be removed;
    # the slot is freed only after its network is deleted, so the next sandbox
    # in that slot can't collide with a leftover veth; and the folder goes last
    # because it holds the PID file.
    validate_sandbox_id(sandbox_id)
    n = find_slot(sandbox_id)          # None if create failed before claiming a slot
    kill_firecracker(sandbox_id)
    remove_cgroup(sandbox_id)
    if n is not None:
        delete_network(n)
        release_slot(n, sandbox_id)
    remove_sandbox_dir(sandbox_id)


# ---------------------------------------------------------------------------
# For the API server (1g): checking what is on the host
#
# On startup the API server compares its database with the host:
#   - a DB row whose sandbox is no longer running -> destroy_sandbox, mark destroyed
#   - a sandbox on the host with no live DB row    -> destroy_sandbox (orphan)
# Every namespace holds a slot while it exists (the slot is freed only after
# the network is deleted), so checking slots and folders also finds leftover
# namespaces.
# ---------------------------------------------------------------------------

def is_sandbox_running(sandbox_id: str) -> bool:
    """Return True if the sandbox's Firecracker process is still alive."""
    validate_sandbox_id(sandbox_id)
    pid = load_pid(sandbox_id)
    return pid is not None and pid_still_belongs_to_sandbox(pid, sandbox_id)


def list_host_sandboxes() -> list[str]:
    """Return the IDs of every sandbox that has a folder or holds a slot on this host."""
    ids = set()
    if os.path.isdir(SANDBOXES_ROOT):
        ids.update(name for name in os.listdir(SANDBOXES_ROOT) if SANDBOX_ID_PATTERN.fullmatch(name))
    if os.path.isdir(slots_dir()):
        for name in os.listdir(slots_dir()):
            try:
                target = os.readlink(f"{slots_dir()}/{name}")
            except OSError:
                continue
            if SANDBOX_ID_PATTERN.fullmatch(target):
                ids.add(target)
    return sorted(ids)


# ---------------------------------------------------------------------------
# Talking to the guest agent: exec, read_file, write_file
# ---------------------------------------------------------------------------

# Note: this name hides Python's built-in exec() in this module; kept because the plan and API use it.
def exec(sandbox_id: str, cmd: str, cwd: str = "/root",
         background: bool = False, timeout: int = 600) -> dict:
    """Run a shell command in the sandbox; return {exit_code, stdout, stderr}, or {pid} if background."""
    body = {"cmd": cmd, "cwd": cwd, "background": background, "timeout": timeout}
    response = httpx.post(
        guest_agent_url(sandbox_id, "/exec"),
        json=body,
        timeout=timeout + EXEC_HTTP_MARGIN,   # let the agent's own timeout fire first
    )
    response.raise_for_status()
    return response.json()


def read_file(sandbox_id: str, path: str) -> str:
    """Return a file's contents from inside the sandbox; raise FileNotFoundError if it's missing."""
    response = httpx.get(guest_agent_url(sandbox_id, "/files"), params={"path": path})
    if response.status_code == 404:
        raise FileNotFoundError(f"{sandbox_id}:{path}")
    response.raise_for_status()
    return response.text


def write_file(sandbox_id: str, path: str, content: str) -> None:
    """Write content to a file inside the sandbox, creating parent folders if needed."""
    response = httpx.put(
        guest_agent_url(sandbox_id, "/files"),
        params={"path": path},
        content=content.encode(),
    )
    response.raise_for_status()
