import pytest
from fastapi.testclient import TestClient

from api import db, observability
from api.main import app
from api.models import Event, EventType, SandboxState, TurnState


SANDBOX_ID = "sbx_" + "b" * 32
ADMIN_KEY = "test-admin-key"
NOW = 1_000_000


## HELPERS ##

def make_events(*steps: tuple) -> list[Event]:
    """Builds events from (type, data, at) tuples, numbered 1, 2, 3, ..."""
    return [
        Event(seq=seq, type=kind, data=data, at=at)
        for seq, (kind, data, at) in enumerate(steps, start=1)
    ]


# a turn that runs pytest, then the agent exits 1 while the command is still out
CRASHED_MID_TOOL = (
    (EventType.USER_MESSAGE, {"text": "fix the tests"}, NOW),
    (EventType.TURN_STARTED, {}, NOW),
    (EventType.LLM_REQUEST, {"model": "gpt-4.1"}, NOW + 1),
    (EventType.LLM_RESPONSE, {"tool_calls": [{"tool_call_id": "c1"}]}, NOW + 2),
    (EventType.TOOL_STARTED, {"tool_call_id": "c1", "name": "run_command", "arguments": {"cmd": "pytest"}}, NOW + 3),
    (EventType.TURN_FAILED, {"reason": "exit", "cause": "agent", "exit_code": 1, "stderr_tail": "Traceback"}, NOW + 9),
)

FINISHED = (
    (EventType.USER_MESSAGE, {"text": "hi"}, NOW),
    (EventType.TURN_STARTED, {}, NOW),
    (EventType.AGENT_OUTPUT, {"text": "hello"}, NOW + 1),
    (EventType.TURN_FINISHED, {"exit_code": 0}, NOW + 2),
)


## GROUPING ##

def test_each_user_message_starts_a_turn():
    events = make_events(
        (EventType.PAUSED, {}, NOW),            # before any message: in no turn
        *FINISHED,
        (EventType.PAUSED, {}, NOW + 5),        # after a turn: stays with it
        (EventType.USER_MESSAGE, {"text": "again"}, NOW + 6),
    )

    turns = observability.split_into_turns(events)

    assert [[event.seq for event in turn] for turn in turns] == [[2, 3, 4, 5, 6], [7]]
    assert observability.events_in_turn(events, 2)[0].data == {"text": "again"}
    assert observability.events_in_turn(events, 3) is None


## SUMMARIES ##

def test_a_finished_turn():
    [summary] = observability.summarize_turns(make_events(*FINISHED), agent_working=False, now=NOW + 10)

    assert summary.status == "finished"
    assert summary.message == "hi"
    assert summary.cause is None
    assert summary.last_action.data == {"text": "hello"}
    assert summary.unfinished_step is None
    assert (summary.started_at, summary.ended_at) == (NOW, NOW + 2)


def test_a_crash_shows_the_cause_and_the_step_that_never_finished():
    [summary] = observability.summarize_turns(make_events(*CRASHED_MID_TOOL), agent_working=False, now=NOW + 10)

    assert summary.status == "failed"
    assert (summary.reason, summary.cause, summary.exit_code) == ("exit", "agent", 1)
    assert summary.stderr_tail == "Traceback"
    assert summary.last_action.type == EventType.TOOL_STARTED
    assert summary.unfinished_step.data["arguments"] == {"cmd": "pytest"}


def test_a_request_with_no_response_is_unfinished():
    events = make_events(
        (EventType.USER_MESSAGE, {"text": "hi"}, NOW),
        (EventType.LLM_REQUEST, {"model": "gpt-4.1"}, NOW + 1),
    )

    [summary] = observability.summarize_turns(events, agent_working=True, now=NOW + 2)

    assert summary.status == "running"
    assert summary.unfinished_step.type == EventType.LLM_REQUEST


def test_a_quiet_working_turn_is_unresponsive():
    events = make_events((EventType.USER_MESSAGE, {"text": "hi"}, NOW))
    later = NOW + observability.UNRESPONSIVE_AFTER

    [summary] = observability.summarize_turns(events, agent_working=True, now=later)

    assert summary.status == "unresponsive"
    assert summary.seconds_since_last_event == observability.UNRESPONSIVE_AFTER


def test_only_the_newest_turn_can_be_running():
    events = make_events((EventType.USER_MESSAGE, {"text": "a"}, NOW), (EventType.USER_MESSAGE, {"text": "b"}, NOW))

    first, second = observability.summarize_turns(events, agent_working=True, now=NOW)

    assert (first.status, second.status) == ("incomplete", "running")


@pytest.mark.parametrize(("reason", "status", "cause"), [
    ("paused", "paused", "paused"),
    ("server restarted", "failed", "host"),
    ("agent failed to start: no such file", "failed", "host"),
    ("timeout", "failed", None),       # older events, from before the runner stored a cause
])
def test_reasons_without_a_stored_cause(reason, status, cause):
    events = make_events(
        (EventType.USER_MESSAGE, {"text": "hi"}, NOW),
        (EventType.TURN_FAILED, {"reason": reason}, NOW + 1),
    )

    [summary] = observability.summarize_turns(events, agent_working=False, now=NOW + 1)

    assert (summary.status, summary.cause) == (status, cause)


## ENDPOINTS ##

@pytest.fixture
def client(tmp_path, monkeypatch):
    """The API with one destroyed sandbox that has two turns, the second one crashed."""
    monkeypatch.setenv("SBX_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("SANDBOX_ADMIN_KEY", ADMIN_KEY)
    db.create_tables()
    db.insert_sandbox(SANDBOX_ID)
    db.set_state(SANDBOX_ID, SandboxState.DESTROYED)
    for kind, data, _ in FINISHED + CRASHED_MID_TOOL:
        db.append_event(SANDBOX_ID, kind, data)

    with TestClient(app, headers={"Authorization": f"Bearer {ADMIN_KEY}"}) as test_client:
        yield test_client


def test_turns_endpoint_sums_up_every_turn_after_destroy(client):
    response = client.get(f"/sandboxes/{SANDBOX_ID}/turns")

    assert response.status_code == 200
    turns = response.json()["turns"]
    assert [(turn["turn"], turn["message"], turn["status"], turn["cause"]) for turn in turns] == [
        (1, "hi", "finished", None),
        (2, "fix the tests", "failed", "agent"),
    ]
    assert turns[1]["unfinished_step"]["data"]["arguments"] == {"cmd": "pytest"}


def test_events_endpoint_filters_by_turn(client):
    response = client.get(f"/sandboxes/{SANDBOX_ID}/events", params={"turn": 2, "after": 6})

    assert response.status_code == 200
    assert [event["seq"] for event in response.json()["events"]] == [7, 8, 9, 10]


def test_events_endpoint_unknown_turn_is_not_found(client):
    response = client.get(f"/sandboxes/{SANDBOX_ID}/events", params={"turn": 3})

    assert response.status_code == 404


def test_turns_endpoint_sees_a_working_agent(client):
    db.set_turn_state(SANDBOX_ID, TurnState.WORKING)
    db.append_event(SANDBOX_ID, EventType.USER_MESSAGE, {"text": "one more"})

    turns = client.get(f"/sandboxes/{SANDBOX_ID}/turns").json()["turns"]

    assert turns[-1]["status"] == "running"
