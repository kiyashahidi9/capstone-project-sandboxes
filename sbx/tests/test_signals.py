"""Tests for cutting fetched events and samples into one observation window."""

from datetime import datetime, timedelta, timezone

from control.signals import get_observation_window
from control.types import AgentEvent, EventType, Policy, VmSample, VmState


UTC = timezone.utc
NOW = datetime(2026, 11, 16, 15, 10, 0, tzinfo=UTC)
POLICY = Policy()


def test_window_drops_rows_after_now() -> None:
    events = [_event("evt_future", NOW + timedelta(seconds=1))]
    samples = [_sample(NOW + timedelta(seconds=5))]

    window = get_observation_window(events, samples, NOW, POLICY)

    assert window.as_of == NOW
    assert window.events == []
    assert window.samples == []


def test_window_drops_rows_older_than_the_policy_window() -> None:
    too_old = NOW - POLICY.window - timedelta(seconds=1)
    events = [_event("evt_old", too_old)]
    samples = [_sample(too_old)]

    window = get_observation_window(events, samples, NOW, POLICY)

    assert window.events == []
    assert window.samples == []


def test_window_keeps_a_row_exactly_at_the_cutoff() -> None:
    cutoff = NOW - POLICY.window
    event = _event("evt_edge", cutoff)
    sample = _sample(cutoff)

    window = get_observation_window([event], [sample], NOW, POLICY)

    assert window.events == [event]
    assert window.samples == [sample]


def test_window_sorts_events_by_time_then_event_id() -> None:
    later = _event("evt_b", NOW - timedelta(seconds=1))
    earlier_b = _event("evt_b", NOW - timedelta(seconds=10))
    earlier_a = _event("evt_a", NOW - timedelta(seconds=10))

    window = get_observation_window([later, earlier_b, earlier_a], [], NOW, POLICY)

    assert [event.event_id for event in window.events] == ["evt_a", "evt_b", "evt_b"]
    assert window.events[0].timestamp < window.events[2].timestamp
    assert window.events[0].event_id < window.events[1].event_id


def test_window_sorts_samples_by_time() -> None:
    newest = _sample(NOW)
    oldest = _sample(NOW - timedelta(seconds=10))

    window = get_observation_window([], [newest, oldest], NOW, POLICY)

    assert window.samples == [oldest, newest]


def test_empty_inputs_return_an_empty_window() -> None:
    window = get_observation_window([], [], NOW, POLICY)

    assert window.as_of == NOW
    assert window.events == []
    assert window.samples == []


def _event(event_id: str, instant: datetime) -> AgentEvent:
    return AgentEvent(event_id=event_id, timestamp=instant, event_type=EventType.AGENT_STARTED)


def _sample(instant: datetime) -> VmSample:
    return VmSample(
        timestamp=instant,
        cpu_percent=0.1,
        network_rx_bytes_delta=0,
        network_tx_bytes_delta=0,
        vm_state=VmState.RUNNING,
    )
