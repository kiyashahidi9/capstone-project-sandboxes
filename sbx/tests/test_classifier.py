"""Tests for classifying one observation window. The decider is not involved."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from control.classifier import classify
from control.types import Activity, AgentEvent, EventType, ObservationWindow, SessionFixture, VmSample, VmState


FIXTURE = Path(__file__).parent / "fixtures" / "session_001.json"
UTC = timezone.utc
T0 = datetime(2026, 11, 16, 15, 0, 0, tzinfo=UTC)


def test_no_events_is_unknown() -> None:
    activity, reason = classify(_window([]))

    assert activity is Activity.UNKNOWN
    assert reason


def test_low_cpu_without_events_is_unknown() -> None:
    """A quiet machine is not idle when the agent events are missing."""
    window = _window([], samples=[_sample(T0, cpu_percent=0.1)])

    activity, _reason = classify(window)

    assert activity is Activity.UNKNOWN


def test_open_tool_is_working_even_when_the_agent_is_waiting_and_cpu_is_low() -> None:
    events = [
        _event("evt_1", 0, EventType.TOOL_STARTED, {"tool_call_id": "call_001"}),
        _event("evt_2", 1, EventType.AGENT_WAITING_FOR_USER),
    ]
    window = _window(events, samples=[_sample(T0 + timedelta(seconds=1), cpu_percent=0.1)])

    activity, _reason = classify(window)

    assert activity is Activity.WORKING


def test_completed_tool_then_waiting_for_the_user_is_waiting() -> None:
    events = [
        _event("evt_1", 0, EventType.TOOL_STARTED, {"tool_call_id": "call_001"}),
        _event("evt_2", 1, EventType.TOOL_COMPLETED, {"tool_call_id": "call_001"}),
        _event("evt_3", 2, EventType.AGENT_WAITING_FOR_USER),
    ]

    activity, _reason = classify(_window(events))

    assert activity is Activity.WAITING


def test_completed_agent_with_no_open_tool_is_idle() -> None:
    events = [
        _event("evt_1", 0, EventType.AGENT_STARTED),
        _event("evt_2", 1, EventType.AGENT_COMPLETED),
    ]

    activity, _reason = classify(_window(events))

    assert activity is Activity.IDLE


def test_failed_agent_with_no_open_tool_is_idle() -> None:
    events = [
        _event("evt_1", 0, EventType.AGENT_STARTED),
        _event("evt_2", 1, EventType.AGENT_FAILED),
    ]

    activity, _reason = classify(_window(events))

    assert activity is Activity.IDLE


def test_completed_agent_does_not_close_an_open_tool() -> None:
    events = [
        _event("evt_1", 0, EventType.TOOL_STARTED, {"tool_call_id": "call_001"}),
        _event("evt_2", 1, EventType.AGENT_COMPLETED),
    ]

    activity, _reason = classify(_window(events))

    assert activity is Activity.WORKING


def test_completion_for_a_different_tool_leaves_the_first_open() -> None:
    events = [
        _event("evt_1", 0, EventType.TOOL_STARTED, {"tool_call_id": "call_001"}),
        _event("evt_2", 1, EventType.TOOL_COMPLETED, {"tool_call_id": "call_002"}),
    ]

    activity, _reason = classify(_window(events))

    assert activity is Activity.WORKING


def test_agent_started_with_no_later_completion_or_waiting_is_working() -> None:
    events = [_event("evt_1", 0, EventType.AGENT_STARTED)]

    activity, _reason = classify(_window(events))

    assert activity is Activity.WORKING


def test_user_instruction_with_no_later_completion_or_waiting_is_working() -> None:
    events = [_event("evt_1", 0, EventType.USER_INSTRUCTION, {"message": "Add 4 + 6"})]

    activity, _reason = classify(_window(events))

    assert activity is Activity.WORKING


def test_a_new_user_instruction_after_completion_is_working() -> None:
    events = [
        _event("evt_1", 0, EventType.AGENT_COMPLETED),
        _event("evt_2", 1, EventType.USER_INSTRUCTION, {"message": "Do the next thing"}),
    ]

    activity, _reason = classify(_window(events))

    assert activity is Activity.WORKING


def test_events_that_never_say_whether_the_turn_finished_are_unknown() -> None:
    events = [_event("evt_1", 0, EventType.LLM_RESPONSE, {"content": "Working on it."})]

    activity, _reason = classify(_window(events))

    assert activity is Activity.UNKNOWN


def test_an_unmatched_http_request_does_not_keep_a_finished_agent_working() -> None:
    events = [
        _event("evt_1", 0, EventType.AGENT_COMPLETED),
        _event("evt_2", 1, EventType.HTTP_REQUEST, {"request_id": "http_001"}),
    ]

    activity, _reason = classify(_window(events))

    assert activity is Activity.IDLE


def test_an_unmatched_llm_request_does_not_keep_a_finished_agent_working() -> None:
    events = [
        _event("evt_1", 0, EventType.AGENT_COMPLETED),
        _event("evt_2", 1, EventType.LLM_REQUEST, {"model": "mock-model"}),
    ]

    activity, _reason = classify(_window(events))

    assert activity is Activity.IDLE


def test_session_fixture_ends_waiting_for_the_user() -> None:
    session = SessionFixture.model_validate_json(FIXTURE.read_text())
    window = ObservationWindow(
        as_of=session.events[-1].timestamp,
        events=session.events,
        samples=session.vm_metrics,
    )

    activity, _reason = classify(window)

    assert activity is Activity.WAITING


def _window(events: list[AgentEvent], samples: list[VmSample] | None = None) -> ObservationWindow:
    as_of: datetime = events[-1].timestamp if events else T0
    return ObservationWindow(as_of=as_of, events=events, samples=samples or [])


def _event(event_id: str, offset_seconds: int, event_type: EventType, data: dict | None = None) -> AgentEvent:
    return AgentEvent(
        event_id=event_id,
        timestamp=T0 + timedelta(seconds=offset_seconds),
        event_type=event_type,
        data=data,
    )


def _sample(instant: datetime, cpu_percent: float) -> VmSample:
    return VmSample(
        timestamp=instant,
        cpu_percent=cpu_percent,
        network_rx_bytes_delta=0,
        network_tx_bytes_delta=0,
        vm_state=VmState.RUNNING,
    )
