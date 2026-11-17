"""Acceptance tests. classify names the agent. decide names the sandbox's next state. replay only walks recorded samples."""

from datetime import timedelta
from pathlib import Path

from control.classifier import classify
from control.decider import decide
from control.types import Activity, AgentEvent, Decision, EventType, ObservationWindow, Policy, SessionFixture, VmState
from tools.replay import replay


FIXTURE = Path(__file__).parent / "fixtures" / "session_001.json"
POLICY = Policy()


def test_an_open_tool_never_pauses() -> None:
    session = _session()
    started = [event for event in session.events if event.event_id <= "evt_005"]
    window = ObservationWindow(
        as_of=session.vm_metrics[-1].timestamp,
        events=started,
        samples=session.vm_metrics,
    )

    activity, _reason = classify(window)
    decision, _reason = decide(activity, window, POLICY)

    assert activity is Activity.WORKING
    assert decision is Decision.KEEP_RUNNING


def test_waiting_shorter_than_30_seconds_keeps_running() -> None:
    session = _session()
    short_metrics = [sample for sample in session.vm_metrics if sample.timestamp.second <= 15]
    window = ObservationWindow(
        as_of=short_metrics[-1].timestamp,
        events=session.events,
        samples=short_metrics,
    )

    activity, _reason = classify(window)
    decision, _reason = decide(activity, window, POLICY)

    assert activity is Activity.WAITING
    assert decision is Decision.KEEP_RUNNING


def test_waiting_for_30_quiet_seconds_can_pause() -> None:
    session = _session()
    window = ObservationWindow(
        as_of=session.vm_metrics[-1].timestamp,
        events=session.events,
        samples=session.vm_metrics,
    )

    activity, _reason = classify(window)
    decision, _reason = decide(activity, window, POLICY)

    assert activity is Activity.WAITING
    assert decision is Decision.PAUSE


def test_missing_events_do_not_pause() -> None:
    session = _session()
    window = ObservationWindow(
        as_of=session.vm_metrics[-1].timestamp,
        events=[],
        samples=session.vm_metrics,
    )

    activity, _reason = classify(window)
    decision, _reason = decide(activity, window, POLICY)

    assert activity is Activity.UNKNOWN
    assert decision is Decision.KEEP_RUNNING


def test_missing_metrics_do_not_pause() -> None:
    session = _session()
    window = ObservationWindow(
        as_of=session.events[-1].timestamp,
        events=session.events,
        samples=[],
    )

    activity, _reason = classify(window)
    decision, _reason = decide(activity, window, POLICY)

    assert activity is Activity.WAITING
    assert decision is Decision.KEEP_RUNNING


def test_a_new_user_request_while_paused_resumes() -> None:
    session = _session()
    paused = session.vm_metrics[-1].model_copy(update={"vm_state": VmState.PAUSED})
    request = AgentEvent(
        event_id="evt_resume",
        timestamp=paused.timestamp + timedelta(seconds=1),
        event_type=EventType.USER_INSTRUCTION,
        data={"message": "continue"},
    )
    window = ObservationWindow(
        as_of=request.timestamp,
        events=[*session.events, request],
        samples=[*session.vm_metrics[:-1], paused],
    )

    decision, reason = decide(Activity.WORKING, window, POLICY)

    assert decision is Decision.RESUME, reason


def test_replaying_the_fixture_twice_matches() -> None:
    session = _session()
    start = session.vm_metrics[0].timestamp
    end = session.vm_metrics[-1].timestamp

    first = replay(session.vm_metrics, start, end, POLICY.sample_interval)
    second = replay(session.vm_metrics, start, end, POLICY.sample_interval)

    assert first == session.vm_metrics
    assert second == first


def _session() -> SessionFixture:
    return SessionFixture.model_validate_json(FIXTURE.read_text())
