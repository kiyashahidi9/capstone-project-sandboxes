"""Startup cleanup (2a, 1g steps 2-3): rows whose sandbox is gone, and leftovers the API no longer tracks."""
import logging
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import host_manager
import uploads
from api import db, helpers
from api.main import app
from api.models import SandboxState, UploadStatus


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


def test_a_running_sandbox_whose_vm_died_is_destroyed_and_logged_even_with_an_old_snapshot(host, caplog):
    """Checks an old snapshot is never used: it's from before the last resume, so it no longer matches the disk."""
    make_sandbox(host, SandboxState.RUNNING, vm_up=False, snapshot_ok=True)

    with caplog.at_level(logging.WARNING):
        helpers.clean_up_after_restart()

    assert state_of() == SandboxState.DESTROYED
    assert f"lost sandbox {RUNNING_ID}" in caplog.text


@pytest.mark.parametrize("status", [None, UploadStatus.PENDING, UploadStatus.UPLOADING,
                                    UploadStatus.FAILED, UploadStatus.CANCELLED])
def test_startup_uploads_a_paused_sandbox_again_unless_its_upload_finished(host, monkeypatch, status):
    """Checks a restart re-uploads snapshots the shutdown cut off, or that never got uploaded."""
    started = []
    monkeypatch.setattr(uploads, "start_upload", started.append)
    make_sandbox(host, SandboxState.PAUSED, snapshot_ok=True)
    make_sandbox(host, SandboxState.PAUSED, sandbox_id=OTHER_ID, snapshot_ok=True)
    make_sandbox(host, SandboxState.RUNNING, sandbox_id=ORPHAN_ID, vm_up=True)
    if status is not None:
        db.set_upload_status(RUNNING_ID, status)
    db.set_upload_status(OTHER_ID, UploadStatus.DONE)

    helpers.restart_unfinished_uploads()

    assert started == [RUNNING_ID]


def test_server_start_restarts_unfinished_uploads(host, monkeypatch):
    """Checks the server runs the re-upload when it starts."""
    started = []
    monkeypatch.setattr(uploads, "start_upload", started.append)
    make_sandbox(host, SandboxState.PAUSED, snapshot_ok=True)

    with TestClient(app):
        pass

    assert started == [RUNNING_ID]


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


def test_a_paused_sandbox_with_no_local_snapshot_is_kept_if_the_bucket_has_a_copy(host, monkeypatch):
    """Checks startup doesn't destroy a sandbox that resume can still restore from the bucket (7b)."""
    monkeypatch.setattr(uploads, "has_complete_copy", lambda sandbox_id: True)
    make_sandbox(host, SandboxState.PAUSED, vm_up=False, snapshot_ok=False)

    helpers.clean_up_after_restart()

    assert state_of() == SandboxState.PAUSED
    assert host.destroyed == []


def test_a_paused_sandbox_is_kept_if_the_bucket_cannot_be_checked(host, monkeypatch):
    """Checks an unreachable bucket never gets a sandbox destroyed on a guess."""
    def unreachable(sandbox_id):
        raise ConnectionError("bucket unreachable")

    monkeypatch.setattr(uploads, "has_complete_copy", unreachable)
    make_sandbox(host, SandboxState.PAUSED, vm_up=False, snapshot_ok=False)

    helpers.clean_up_after_restart()

    assert state_of() == SandboxState.PAUSED


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


# ---------------------------------------------------------------------------
# Deleting forgotten sandboxes (6b): off unless DELETE_PAUSED_AFTER_DAYS is set
# ---------------------------------------------------------------------------

NOW = 1_800_000_000
DAY = 24 * 60 * 60
LIMIT_DAYS = 30


def paused_days_ago(host, sandbox_id, days):
    """Records a paused sandbox whose pause happened the given number of days before NOW."""
    make_sandbox(host, SandboxState.PAUSED, sandbox_id=sandbox_id, snapshot_ok=True)
    db.transition(sandbox_id, [SandboxState.PAUSED], SandboxState.PAUSED, paused_at=NOW - days * DAY)


@pytest.fixture
def fake_clock(monkeypatch):
    """Freezes the database's clock at NOW, so ages in days are exact."""
    monkeypatch.setattr(db, "now", lambda: NOW)


def test_a_sandbox_paused_longer_than_the_limit_is_deleted(host, fake_clock, monkeypatch):
    """Checks only paused sandboxes past the limit go, and running ones are never touched."""
    monkeypatch.setenv(helpers.DELETE_AFTER_ENV, str(LIMIT_DAYS))
    paused_days_ago(host, RUNNING_ID, LIMIT_DAYS + 1)
    paused_days_ago(host, OTHER_ID, LIMIT_DAYS - 1)
    make_sandbox(host, SandboxState.RUNNING, sandbox_id=ORPHAN_ID, vm_up=True)

    helpers.delete_forgotten_sandboxes()

    assert state_of(RUNNING_ID) == SandboxState.DESTROYED
    assert state_of(OTHER_ID) == SandboxState.PAUSED
    assert state_of(ORPHAN_ID) == SandboxState.RUNNING


@pytest.mark.parametrize("setting", ["", "thirty", "0", "-3"])
def test_nothing_is_deleted_when_the_setting_is_blank_or_invalid(host, fake_clock, monkeypatch, setting):
    """Checks the default (blank) and a typo both delete nothing."""
    monkeypatch.setenv(helpers.DELETE_AFTER_ENV, setting)
    paused_days_ago(host, RUNNING_ID, 10_000)

    helpers.delete_forgotten_sandboxes()

    assert state_of(RUNNING_ID) == SandboxState.PAUSED


def test_the_cleanup_loop_runs_until_the_server_stops(monkeypatch):
    """Checks the hourly loop runs a pass at once and ends when told to stop."""
    stop = threading.Event()
    passes = []

    def one_pass():
        passes.append(1)
        stop.set()

    monkeypatch.setattr(helpers, "delete_forgotten_sandboxes", one_pass)

    helpers.run_cleanup_until_stopped(stop)

    assert passes == [1]
