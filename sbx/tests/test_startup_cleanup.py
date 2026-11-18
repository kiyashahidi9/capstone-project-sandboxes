"""Startup cleanup (2a, 1g steps 2-3): rows whose sandbox is gone, and leftovers the API no longer tracks."""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import host_manager
from api import db, helpers
from api.main import app
from api.models import SandboxState


RUNNING_ID = "sbx_" + "a" * 32
OTHER_ID = "sbx_" + "b" * 32
ORPHAN_ID = "sbx_" + "c" * 32
ADMIN_KEY = "test-admin-key"


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """Points every test at its own temporary database."""
    monkeypatch.setenv("SBX_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("SANDBOX_ADMIN_KEY", ADMIN_KEY)   # the server won't start without it
    db.create_tables()


@pytest.fixture
def host(monkeypatch):
    """Pretends to be the host, with each sandbox's VM, snapshot and files tracked on their own."""
    fake = SimpleNamespace(on_host=set(), vm_up=set(), snapshot_ok=set(), destroyed=[], broken=set())

    def destroy_sandbox(sandbox_id):
        if sandbox_id in fake.broken:
            raise RuntimeError(f"can't remove {sandbox_id}")
        fake.destroyed.append(sandbox_id)
        fake.on_host.discard(sandbox_id)
        fake.vm_up.discard(sandbox_id)

    def require_resumable_files(sandbox_id):
        if sandbox_id not in fake.snapshot_ok:
            raise FileNotFoundError("no snapshot")

    monkeypatch.setattr(host_manager, "list_host_sandboxes", lambda: sorted(fake.on_host))
    monkeypatch.setattr(host_manager, "is_sandbox_running", lambda sandbox_id: sandbox_id in fake.vm_up)
    monkeypatch.setattr(host_manager, "sandbox_manager_is_healthy", lambda sandbox_id: sandbox_id in fake.vm_up)
    monkeypatch.setattr(host_manager, "require_resumable_files", require_resumable_files)
    monkeypatch.setattr(host_manager, "check_snapshot_files", lambda sandbox_id: None)
    monkeypatch.setattr(host_manager, "kill_firecracker", lambda sandbox_id: fake.vm_up.discard(sandbox_id))
    monkeypatch.setattr(host_manager, "destroy_sandbox", destroy_sandbox)
    monkeypatch.setattr(helpers, "try_unfreeze", lambda sandbox_id: None)
    return fake


def make_sandbox(host, state, sandbox_id=RUNNING_ID, vm_up=False, snapshot_ok=False):
    """Records a sandbox in the given state, with its files on the host and its VM up or not."""
    db.insert_sandbox(sandbox_id)
    db.set_state(sandbox_id, state)
    host.on_host.add(sandbox_id)
    if vm_up:
        host.vm_up.add(sandbox_id)
    if snapshot_ok:
        host.snapshot_ok.add(sandbox_id)
    return sandbox_id


def state_of(sandbox_id=RUNNING_ID):
    """Gives a sandbox's current state."""
    return db.get_sandbox(sandbox_id).state


# ---------------------------------------------------------------------------
# Rows whose sandbox is gone (step 2)
# ---------------------------------------------------------------------------

def test_a_running_sandbox_with_its_vm_up_is_kept(host):
    make_sandbox(host, SandboxState.RUNNING, vm_up=True)

    helpers.clean_up_after_restart()

    assert state_of() == SandboxState.RUNNING
    assert host.destroyed == []


def test_a_running_sandbox_whose_vm_died_is_destroyed(host):
    make_sandbox(host, SandboxState.RUNNING, vm_up=False)

    helpers.clean_up_after_restart()

    assert state_of() == SandboxState.DESTROYED
    assert host.destroyed == [RUNNING_ID]


@pytest.mark.parametrize("vm_up", [False, True], ids=["vm-down", "vm-up"])
def test_a_sandbox_stuck_starting_is_destroyed(host, vm_up):
    """A crash cut off the create, so nobody was ever given this sandbox's ID."""
    make_sandbox(host, SandboxState.STARTING, vm_up=vm_up)

    helpers.clean_up_after_restart()

    assert state_of() == SandboxState.DESTROYED
    assert host.destroyed == [RUNNING_ID]


def test_a_paused_sandbox_with_a_full_snapshot_is_kept(host):
    """A paused sandbox has no VM on purpose; destroying it would throw away the user's work."""
    make_sandbox(host, SandboxState.PAUSED, vm_up=False, snapshot_ok=True)

    helpers.clean_up_after_restart()

    assert state_of() == SandboxState.PAUSED
    assert host.destroyed == []


def test_a_paused_sandbox_whose_snapshot_is_missing_is_destroyed(host):
    """It can never be resumed, so keeping it would only hold a slot and fail every resume."""
    make_sandbox(host, SandboxState.PAUSED, vm_up=False, snapshot_ok=False)

    helpers.clean_up_after_restart()

    assert state_of() == SandboxState.DESTROYED
    assert host.destroyed == [RUNNING_ID]


# ---------------------------------------------------------------------------
# Leftovers the API no longer tracks (step 3)
# ---------------------------------------------------------------------------

def test_a_sandbox_on_the_host_with_no_row_is_destroyed(host):
    host.on_host.add(ORPHAN_ID)

    helpers.clean_up_after_restart()

    assert host.destroyed == [ORPHAN_ID]


def test_a_destroyed_row_whose_files_are_still_on_the_host_is_cleaned_up(host):
    make_sandbox(host, SandboxState.DESTROYED)

    helpers.clean_up_after_restart()

    assert host.destroyed == [RUNNING_ID]
    assert state_of() == SandboxState.DESTROYED


def test_each_leftover_is_destroyed_once(host):
    """A row destroyed in step 2 is no longer on the host, so step 3 doesn't destroy it again."""
    make_sandbox(host, SandboxState.RUNNING, vm_up=False)
    host.on_host.add(ORPHAN_ID)

    helpers.clean_up_after_restart()

    assert sorted(host.destroyed) == [RUNNING_ID, ORPHAN_ID]


# ---------------------------------------------------------------------------
# Failures and order
# ---------------------------------------------------------------------------

def test_one_failed_destroy_does_not_stop_the_others(host):
    """One broken sandbox mustn't keep the API down, or every other sandbox is unreachable too."""
    make_sandbox(host, SandboxState.RUNNING, sandbox_id=RUNNING_ID, vm_up=False)
    make_sandbox(host, SandboxState.RUNNING, sandbox_id=OTHER_ID, vm_up=False)
    host.broken.add(RUNNING_ID)

    helpers.clean_up_after_restart()

    assert host.destroyed == [OTHER_ID]
    assert state_of(OTHER_ID) == SandboxState.DESTROYED


def test_server_start_keeps_a_pause_that_finished_before_the_crash(host):
    """The pausing row is settled to paused first, so the cleanup then keeps it."""
    make_sandbox(host, SandboxState.PAUSING, vm_up=False, snapshot_ok=True)

    with TestClient(app):
        pass

    assert state_of() == SandboxState.PAUSED
    assert host.destroyed == []


def test_server_start_destroys_a_sandbox_whose_vm_died(host):
    make_sandbox(host, SandboxState.RUNNING, vm_up=False)
    host.on_host.add(ORPHAN_ID)

    with TestClient(app):
        pass

    assert state_of() == SandboxState.DESTROYED
    assert sorted(host.destroyed) == [RUNNING_ID, ORPHAN_ID]


def test_unit_tests_never_point_at_the_real_sandbox_folder():
    """Guards conftest.py: with the real /var/sbx, sudo pytest on the host would destroy real sandboxes."""
    assert host_manager.SANDBOXES_ROOT != "/var/sbx"
