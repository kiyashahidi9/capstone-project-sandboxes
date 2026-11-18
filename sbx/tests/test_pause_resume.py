import threading
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import locks
import node_agent
from api import db, helpers
from api.main import app
from api.models import SandboxState
from runner import runner


ID = "sbx_" + "a" * 32
OTHER_ID = "sbx_" + "b" * 32
WAIT_SECONDS = 5                  # longest a test waits for another thread before failing
ADMIN_KEY = "test-admin-key"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    """Starts the API on a fresh temporary database and calls it with the admin key, like test_api.py."""
    monkeypatch.setenv("SBX_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("SANDBOX_ADMIN_KEY", ADMIN_KEY)
    db.create_tables()
    with TestClient(app, headers={"Authorization": f"Bearer {ADMIN_KEY}"}) as test_client:
        yield test_client


@pytest.fixture
def host(monkeypatch):
    """Pretends to be the host manager's VM controls, recording every call."""
    fake = SimpleNamespace(
        calls=[], alive=True, healthy=True, snapshot=True,
        pause_error=None, resume_error=None, pause_gate=None,
    )

    def pause_sandbox(sandbox_id):
        fake.calls.append("pause")
        if fake.pause_gate is not None:
            fake.pause_gate.wait(WAIT_SECONDS)
        if fake.pause_error is not None:
            raise fake.pause_error
        fake.alive = False
        return {}

    def resume_sandbox(sandbox_id):
        fake.calls.append("resume")
        if fake.resume_error is not None:
            raise fake.resume_error
        fake.alive = True
        return {}

    def require_resumable_files(sandbox_id):
        if not fake.snapshot:
            raise FileNotFoundError("no snapshot")

    def kill_firecracker(sandbox_id):
        fake.calls.append("kill")
        fake.alive = False

    def destroy_sandbox(sandbox_id):
        fake.calls.append("destroy")
        fake.alive = False

    monkeypatch.setattr(node_agent, "pause_sandbox", pause_sandbox)
    monkeypatch.setattr(node_agent, "resume_sandbox", resume_sandbox)
    monkeypatch.setattr(node_agent, "is_sandbox_running", lambda sandbox_id: fake.alive)
    monkeypatch.setattr(node_agent, "guest_agent_is_healthy", lambda sandbox_id: fake.healthy)
    monkeypatch.setattr(node_agent, "require_resumable_files", require_resumable_files)
    monkeypatch.setattr(node_agent, "check_snapshot_files", lambda sandbox_id: None)
    monkeypatch.setattr(node_agent, "kill_firecracker", kill_firecracker)
    monkeypatch.setattr(node_agent, "destroy_sandbox", destroy_sandbox)
    monkeypatch.setattr(helpers, "try_unfreeze", lambda sandbox_id: fake.calls.append("unfreeze"))
    monkeypatch.setattr(runner, "stop_turn", lambda sandbox_id: fake.calls.append("stop_turn"))
    return fake


def make_sandbox(state, sandbox_id=ID):
    """Records a sandbox already in the given state."""
    db.insert_sandbox(sandbox_id)
    db.set_state(sandbox_id, state)
    return sandbox_id


def state_of(sandbox_id=ID):
    """Gives a sandbox's current state."""
    return db.get_sandbox(sandbox_id).state


def wait_for_state(state, sandbox_id=ID):
    """Waits until a sandbox reaches the given state, failing the test if it never does."""
    deadline = time.monotonic() + WAIT_SECONDS
    while state_of(sandbox_id) != state:
        assert time.monotonic() < deadline, f"never reached {state}"
        time.sleep(0.01)


def run_in_thread(call, *args):
    """Starts a call on its own thread, keeping any HTTP error it raises for the test to check."""
    outcome = SimpleNamespace(error=None)

    def target():
        try:
            call(*args)
        except HTTPException as error:
            outcome.error = error

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    outcome.thread = thread
    return outcome


def finishes(outcome):
    """Waits for a thread from run_in_thread and says whether it ended in time."""
    outcome.thread.join(WAIT_SECONDS)
    return not outcome.thread.is_alive()


# ---------------------------------------------------------------------------
# Pause and resume
# ---------------------------------------------------------------------------

def test_pause_then_resume(client, host):
    make_sandbox(SandboxState.RUNNING)

    paused = client.post(f"/sandboxes/{ID}/pause")
    resumed = client.post(f"/sandboxes/{ID}/resume")

    assert paused.status_code == 200
    assert paused.json()["state"] == "paused"
    assert paused.json()["paused_at"] is not None
    assert resumed.status_code == 200
    assert resumed.json()["state"] == "running"
    assert resumed.json()["paused_at"] is None
    assert host.calls == ["stop_turn", "pause", "resume"]


def test_message_to_a_paused_sandbox_resumes_it_then_starts_the_turn(client, host, monkeypatch):
    make_sandbox(SandboxState.PAUSED)
    started = []
    monkeypatch.setattr(runner, "run_turn", lambda sandbox_id: started.append((sandbox_id, state_of(sandbox_id))))

    response = client.post(f"/sandboxes/{ID}/messages", json={"text": "carry on"})

    assert response.status_code == 202
    assert host.calls == ["resume"]
    deadline = time.monotonic() + WAIT_SECONDS
    while not started:
        assert time.monotonic() < deadline, "the turn never started"
        time.sleep(0.01)
    assert started == [(ID, SandboxState.RUNNING)]   # running before the agent starts
    assert not locks.lock_for(ID).locked()           # the resume let the lock go for the runner


def test_message_whose_resume_fails_starts_no_turn(client, host, monkeypatch):
    make_sandbox(SandboxState.PAUSED)
    host.resume_error = RuntimeError("snapshot load failed")
    host.healthy = False
    started = []
    monkeypatch.setattr(runner, "run_turn", started.append)

    response = client.post(f"/sandboxes/{ID}/messages", json={"text": "carry on"})

    assert response.status_code == 500
    assert state_of() == SandboxState.PAUSED
    assert db.get_sandbox(ID).turn_state == "idle"
    assert db.list_events(ID) == []
    assert started == []


def test_pause_records_the_snapshot_folder(client, host):
    make_sandbox(SandboxState.RUNNING)

    client.post(f"/sandboxes/{ID}/pause")

    assert db.get_sandbox(ID).snapshot_dir == node_agent.snapshot_dir(ID)


@pytest.mark.parametrize("state, action", [
    (SandboxState.PAUSED, "pause"),
    (SandboxState.RUNNING, "resume"),
    (SandboxState.DESTROYED, "pause"),
    (SandboxState.DESTROYED, "resume"),
    (SandboxState.STARTING, "pause"),
    (SandboxState.STARTING, "resume"),
])
def test_wrong_state_is_a_conflict(client, host, state, action):
    make_sandbox(state)

    response = client.post(f"/sandboxes/{ID}/{action}")

    assert response.status_code == 409
    assert state_of() == state
    assert "pause" not in host.calls and "resume" not in host.calls


@pytest.mark.parametrize("action", ["pause", "resume"])
def test_unknown_sandbox_is_not_found(client, host, action):
    assert client.post(f"/sandboxes/{ID}/{action}").status_code == 404


def test_pause_cuts_off_the_turn_before_taking_the_lock(client, host, monkeypatch):
    make_sandbox(SandboxState.RUNNING)
    lock_held_at_stop = []
    monkeypatch.setattr(runner, "stop_turn", lambda sandbox_id: lock_held_at_stop.append(locks.lock_for(ID).locked()))

    client.post(f"/sandboxes/{ID}/pause")

    assert lock_held_at_stop == [False]


def test_pause_cuts_off_a_new_turn_that_took_the_lock_first(host, client, monkeypatch):
    make_sandbox(SandboxState.RUNNING)
    monkeypatch.setattr(helpers, "LOCK_RETRY_SECONDS", 0.01)
    turn_lock = locks.lock_for(ID)
    turn_lock.acquire()               # a turn that started right after the first stop_turn
    stops = []

    def stop_turn(sandbox_id):
        stops.append(sandbox_id)
        if len(stops) == 2:           # the retry cuts the new turn off, which frees the lock
            turn_lock.release()

    monkeypatch.setattr(runner, "stop_turn", stop_turn)

    helpers.pause(ID)

    assert len(stops) == 2
    assert state_of() == SandboxState.PAUSED


def test_resume_on_a_running_sandbox_answers_at_once_while_a_turn_holds_the_lock(client, host):
    make_sandbox(SandboxState.RUNNING)

    with locks.lock_for(ID):
        outcome = run_in_thread(helpers.resume, ID)
        assert finishes(outcome)

    assert outcome.error.status_code == 409


# ---------------------------------------------------------------------------
# Failures
# ---------------------------------------------------------------------------

def test_failed_pause_with_the_vm_still_up_goes_back_to_running(client, host):
    make_sandbox(SandboxState.RUNNING)
    host.pause_error = RuntimeError("sync failed")

    response = client.post(f"/sandboxes/{ID}/pause")

    assert response.status_code == 500
    assert state_of() == SandboxState.RUNNING


def test_failed_pause_after_a_full_snapshot_counts_as_paused(client, host):
    make_sandbox(SandboxState.RUNNING)
    host.pause_error = RuntimeError("kill step failed")
    host.alive = False                # the VM is already gone, but its snapshot is complete

    response = client.post(f"/sandboxes/{ID}/pause")

    assert response.status_code == 500
    assert state_of() == SandboxState.PAUSED


def test_failed_pause_with_no_vm_and_no_snapshot_destroys_it(client, host):
    make_sandbox(SandboxState.RUNNING)
    host.pause_error = RuntimeError("crashed")
    host.alive = False
    host.snapshot = False

    response = client.post(f"/sandboxes/{ID}/pause")

    assert response.status_code == 500
    assert state_of() == SandboxState.DESTROYED
    assert "destroy" in host.calls


def test_failed_resume_stays_paused(client, host):
    make_sandbox(SandboxState.RUNNING)
    client.post(f"/sandboxes/{ID}/pause")
    host.resume_error = RuntimeError("load failed")

    response = client.post(f"/sandboxes/{ID}/resume")

    assert response.status_code == 500
    assert state_of() == SandboxState.PAUSED


# ---------------------------------------------------------------------------
# Resume on next request
# ---------------------------------------------------------------------------

def test_exec_on_a_paused_sandbox_resumes_it_first(client, host, monkeypatch):
    make_sandbox(SandboxState.RUNNING)
    client.post(f"/sandboxes/{ID}/pause")
    monkeypatch.setattr(node_agent, "exec", lambda *args, **kwargs: host.calls.append("exec") or {
        "exit_code": 0, "stdout": "", "stderr": "", "timed_out": False,
    })

    response = client.post(f"/sandboxes/{ID}/exec", json={"cmd": "true"})

    assert response.status_code == 200
    assert host.calls[-2:] == ["resume", "exec"]
    assert state_of() == SandboxState.RUNNING


def test_file_calls_on_a_paused_sandbox_resume_it_first(client, host, monkeypatch):
    make_sandbox(SandboxState.RUNNING)
    client.post(f"/sandboxes/{ID}/pause")
    monkeypatch.setattr(node_agent, "write_file", lambda sandbox_id, path, content: host.calls.append("write"))
    monkeypatch.setattr(node_agent, "read_file", lambda sandbox_id, path: "hi")

    written = client.put(f"/sandboxes/{ID}/files", params={"path": "/root/a"}, content=b"hi")
    client.post(f"/sandboxes/{ID}/pause")
    read = client.get(f"/sandboxes/{ID}/files", params={"path": "/root/a"})

    assert written.status_code == 200
    assert read.text == "hi"
    assert host.calls.count("resume") == 2


def test_exec_on_a_running_sandbox_does_not_wait_for_the_lock(client, host):
    make_sandbox(SandboxState.RUNNING)

    with locks.lock_for(ID):          # the agent's turn holds it while the agent calls exec
        outcome = run_in_thread(helpers.ensure_running, ID)
        assert finishes(outcome)

    assert outcome.error is None


def test_exec_on_a_destroyed_sandbox_is_a_conflict(client, host):
    make_sandbox(SandboxState.DESTROYED)

    response = client.post(f"/sandboxes/{ID}/exec", json={"cmd": "true"})

    assert response.status_code == 409


# ---------------------------------------------------------------------------
# One change at a time
# ---------------------------------------------------------------------------

def test_resume_sent_during_a_pause_waits_then_resumes(client, host):
    make_sandbox(SandboxState.RUNNING)
    host.pause_gate = threading.Event()

    pausing = run_in_thread(helpers.pause, ID)
    wait_for_state(SandboxState.PAUSING)
    resuming = run_in_thread(helpers.resume, ID)
    time.sleep(0.05)
    waited = resuming.thread.is_alive()
    host.pause_gate.set()

    assert finishes(pausing) and finishes(resuming)
    assert waited
    assert resuming.error is None
    assert host.calls[-2:] == ["pause", "resume"]
    assert state_of() == SandboxState.RUNNING


def test_delete_during_a_pause_waits_then_destroys(client, host):
    make_sandbox(SandboxState.RUNNING)
    host.pause_gate = threading.Event()

    pausing = run_in_thread(helpers.pause, ID)
    wait_for_state(SandboxState.PAUSING)
    deleting = run_in_thread(helpers.destroy, ID)
    time.sleep(0.05)
    waited = deleting.thread.is_alive()
    host.pause_gate.set()

    assert finishes(pausing) and finishes(deleting)
    assert waited
    assert [call for call in host.calls if call != "stop_turn"] == ["pause", "destroy"]
    assert state_of() == SandboxState.DESTROYED


def test_delete_a_paused_sandbox(client, host):
    make_sandbox(SandboxState.RUNNING)
    client.post(f"/sandboxes/{ID}/pause")

    response = client.delete(f"/sandboxes/{ID}")

    assert response.status_code == 200
    assert host.calls[-1] == "destroy"
    assert state_of() == SandboxState.DESTROYED


def test_different_sandboxes_do_not_wait_for_each_other(client, host):
    make_sandbox(SandboxState.RUNNING)
    make_sandbox(SandboxState.PAUSED, OTHER_ID)

    with locks.lock_for(ID):
        outcome = run_in_thread(helpers.resume, OTHER_ID)
        assert finishes(outcome)

    assert state_of(OTHER_ID) == SandboxState.RUNNING


# ---------------------------------------------------------------------------
# After a crash
# ---------------------------------------------------------------------------

def test_startup_settles_a_pause_cut_off_while_the_vm_still_runs(host):
    make_sandbox(SandboxState.PAUSING)

    helpers.recover_interrupted_changes()

    assert host.calls == ["unfreeze"]
    assert state_of() == SandboxState.RUNNING


def test_startup_settles_a_pause_cut_off_after_a_full_snapshot(host):
    make_sandbox(SandboxState.PAUSING)
    host.alive = False

    helpers.recover_interrupted_changes()

    assert state_of() == SandboxState.PAUSED
    assert db.get_sandbox(ID).paused_at is not None


def test_startup_stops_a_stuck_vm_and_uses_its_snapshot(host):
    make_sandbox(SandboxState.PAUSING)
    host.healthy = False              # frozen for good: alive but not answering

    helpers.recover_interrupted_changes()

    assert "kill" in host.calls
    assert state_of() == SandboxState.PAUSED


def test_startup_destroys_a_sandbox_with_no_vm_and_no_snapshot(host):
    make_sandbox(SandboxState.PAUSING)
    host.alive = False
    host.snapshot = False

    helpers.recover_interrupted_changes()

    assert state_of() == SandboxState.DESTROYED


def test_startup_settles_a_finished_resume_as_running(host):
    make_sandbox(SandboxState.RESUMING)

    helpers.recover_interrupted_changes()

    assert state_of() == SandboxState.RUNNING


def test_startup_settles_a_half_done_resume_as_paused(host):
    make_sandbox(SandboxState.RESUMING)
    host.healthy = False

    helpers.recover_interrupted_changes()

    assert "kill" in host.calls
    assert state_of() == SandboxState.PAUSED


def test_server_start_runs_the_recovery(tmp_path, monkeypatch, host):
    monkeypatch.setenv("SBX_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("SANDBOX_ADMIN_KEY", ADMIN_KEY)   # the server won't start without it
    db.create_tables()
    make_sandbox(SandboxState.RESUMING)

    with TestClient(app):
        pass

    assert state_of() == SandboxState.RUNNING


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """Points every test at its own temporary database, even the ones without the client."""
    monkeypatch.setenv("SBX_DB", str(tmp_path / "state.db"))
    db.create_tables()
