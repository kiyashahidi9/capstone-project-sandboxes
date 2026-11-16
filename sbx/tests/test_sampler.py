"""Tests for the metric poller. The clock is the timestamp passed to poll."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from control.mocks import FakeNodeAgent
from control.sampler import poll, replay
from control.types import Policy, SessionFixture, VmSample, VmState


FIXTURE = Path(__file__).parent / "fixtures" / "session_001.json"
UTC = timezone.utc


@pytest.fixture
def session() -> SessionFixture:
    return SessionFixture.model_validate_json(FIXTURE.read_text())


@pytest.fixture
def agent(session) -> FakeNodeAgent:
    return FakeNodeAgent.from_session(session)


def test_poll_returns_the_sample_recorded_at_that_time(session, agent):
    """A poll at a recorded timestamp returns that reading, including a quiet one."""
    first = poll(agent, session.vm_metrics[0].timestamp)
    last = poll(agent, session.vm_metrics[-1].timestamp)

    assert first == session.vm_metrics[0]
    assert first.cpu_percent == 32.5
    assert first.network_rx_bytes_delta == 2048
    assert first.network_tx_bytes_delta == 1024
    assert first.vm_state == VmState.RUNNING

    assert last == session.vm_metrics[-1]
    assert last.cpu_percent == 0.1
    assert last.network_rx_bytes_delta == 0
    assert last.network_tx_bytes_delta == 0


def test_poll_at_a_shared_event_and_sample_time_returns_the_sample(session, agent):
    """15:00:00Z has both a user instruction and a metric. The poll returns the metric."""
    assert poll(agent, session.events[0].timestamp) == session.vm_metrics[0]


def test_poll_every_sample_interval_walks_the_fixture_in_order(session, agent):
    """Stepping the policy interval from the first sample hits each recorded reading once."""
    start = session.vm_metrics[0].timestamp
    interval = Policy().sample_interval

    readings = [poll(agent, start + (i * interval)) for i in range(len(session.vm_metrics))]

    assert readings == session.vm_metrics


def test_poll_at_an_event_time_without_a_metric_returns_none(session, agent):
    """15:00:01Z is an agent event and not a sample time."""
    assert poll(agent, session.events[1].timestamp) is None


@pytest.mark.parametrize(
    "offset",
    [
        timedelta(seconds=-5),
        timedelta(seconds=2),
        timedelta(microseconds=1),
        timedelta(seconds=45),
    ],
)
def test_poll_returns_none_when_that_instant_has_no_sample(session, agent, offset):
    """Before, between, one microsecond after, and after the recording are all misses."""
    instant = session.vm_metrics[0].timestamp + offset
    assert poll(agent, instant) is None


def test_poll_does_not_reuse_an_earlier_sample(agent):
    """One second after a real sample is a miss. The previous reading stays at its own time."""
    earlier = datetime(2026, 11, 16, 15, 0, 10, tzinfo=UTC)
    asked = earlier + timedelta(seconds=1)

    assert poll(agent, earlier).cpu_percent == 0.4
    assert poll(agent, asked) is None


def test_poll_does_not_fill_a_gap():
    """A missing 5-second reading stays missing. The samples on either side do not stand in for it."""
    start = datetime(2026, 11, 16, 15, 0, 0, tzinfo=UTC)
    samples = [
        _sample(start, cpu_percent=10.0),
        _sample(start + timedelta(seconds=10), cpu_percent=1.0),
    ]
    agent = FakeNodeAgent(samples)

    assert poll(agent, start + timedelta(seconds=5)) is None
    assert poll(agent, start).cpu_percent == 10.0
    assert poll(agent, start + timedelta(seconds=10)).cpu_percent == 1.0


def test_poll_returns_none_for_an_empty_provider():
    agent = FakeNodeAgent([])
    assert poll(agent, datetime(2026, 11, 16, 15, 0, 0, tzinfo=UTC)) is None


def test_poll_keeps_a_zero_cpu_reading():
    """CPU at 0% and zero network deltas are a real sample."""
    instant = datetime(2026, 11, 16, 15, 0, 0, tzinfo=UTC)
    sample = _sample(instant, cpu_percent=0.0)
    agent = FakeNodeAgent([sample])

    assert poll(agent, instant) == sample


def test_poll_passes_through_a_paused_vm_state():
    instant = datetime(2026, 11, 16, 15, 0, 0, tzinfo=UTC)
    sample = _sample(instant, vm_state=VmState.PAUSED)
    agent = FakeNodeAgent([sample])

    assert poll(agent, instant).vm_state == VmState.PAUSED


def test_same_timestamp_twice_returns_the_same_sample(session, agent):
    instant = session.vm_metrics[2].timestamp
    assert poll(agent, instant) == poll(agent, instant)


def test_utc_offset_matching_z_is_the_same_instant(session, agent):
    """15:00:00+00:00 and the fixture's 15:00:00Z are one timestamp."""
    same_instant = datetime(2026, 11, 16, 15, 0, 0, tzinfo=timezone(timedelta(0)))
    assert poll(agent, same_instant) == session.vm_metrics[0]


def test_duplicate_timestamp_returns_the_later_sample():
    instant = datetime(2026, 11, 16, 15, 0, 0, tzinfo=UTC)
    first = _sample(instant, cpu_percent=4.0)
    later = _sample(instant, cpu_percent=9.0)
    agent = FakeNodeAgent([first, later])

    assert poll(agent, instant) == later


def test_naive_timestamp_is_rejected(agent):
    naive = datetime(2026, 11, 16, 15, 0, 0)
    with pytest.raises(TypeError):
        poll(agent, naive)


def test_poll_does_not_record_pause_or_resume(session, agent):
    start = session.vm_metrics[0].timestamp
    interval = Policy().sample_interval
    for i in range(len(session.vm_metrics)):
        poll(agent, start + (i * interval))

    assert agent.calls == []


# Replay. These timestamps are in 2020, so the range stays in the past.
PAST = datetime(2020, 1, 1, tzinfo=UTC)
STEP = timedelta(seconds=5)


def test_replay_walks_the_past_and_keeps_missing_steps():
    """Each step calls poll. A time with no sample is None, and the walk stops at end."""
    samples = [
        _sample(PAST, cpu_percent=3.0),
        _sample(PAST + timedelta(seconds=10), cpu_percent=1.0),
    ]
    agent = FakeNodeAgent(samples)
    end = PAST + timedelta(seconds=12)

    readings = replay(agent, PAST, end, STEP)

    assert readings == [samples[0], None, samples[1]]
    assert agent.calls == []


def test_replay_includes_a_range_that_is_one_instant():
    sample = _sample(PAST, cpu_percent=4.0)
    agent = FakeNodeAgent([sample])

    assert replay(agent, PAST, PAST, STEP) == [sample]


def test_replay_with_an_interval_longer_than_the_range_returns_the_start():
    sample = _sample(PAST, cpu_percent=4.0)
    later = _sample(PAST + timedelta(seconds=10), cpu_percent=9.0)
    agent = FakeNodeAgent([sample, later])

    assert replay(agent, PAST, PAST + timedelta(seconds=10), timedelta(hours=1)) == [sample]


def test_replay_of_an_empty_provider_returns_a_none_per_step():
    agent = FakeNodeAgent([])

    assert replay(agent, PAST, PAST + (STEP * 2), STEP) == [None, None, None]


def test_replay_twice_returns_the_same_readings():
    sample = _sample(PAST, cpu_percent=2.0)
    agent = FakeNodeAgent([sample])

    first = replay(agent, PAST, PAST, STEP)
    second = replay(agent, PAST, PAST, STEP)

    assert first == [sample]
    assert second == first


def test_replay_allows_an_end_equal_to_now():
    """end > now is the future. An end equal to the current time is still allowed."""
    current = datetime.now(UTC)
    sample = _sample(current, cpu_percent=1.0)
    agent = FakeNodeAgent([sample])

    assert replay(agent, current, current, STEP) == [sample]


def test_replay_rejects_an_end_in_the_future():
    current = datetime.now(UTC)
    agent = FakeNodeAgent([])

    with pytest.raises(ValueError, match="end is in the future"):
        replay(agent, current - STEP, current + STEP, STEP)


def test_replay_rejects_a_start_after_the_end():
    agent = FakeNodeAgent([])

    with pytest.raises(ValueError, match="start is after end"):
        replay(agent, PAST + STEP, PAST, STEP)


def test_replay_rejects_a_negative_interval():
    agent = FakeNodeAgent([])

    with pytest.raises(ValueError, match="interval is negative"):
        replay(agent, PAST, PAST + STEP, timedelta(seconds=-5))


def test_replay_rejects_a_zero_interval():
    agent = FakeNodeAgent([])

    with pytest.raises(ValueError, match="interval is zero"):
        replay(agent, PAST, PAST + STEP, timedelta(0))


def test_replay_rejects_a_missing_interval():
    agent = FakeNodeAgent([])

    with pytest.raises(ValueError, match="interval is None"):
        replay(agent, PAST, PAST + STEP, None)


@pytest.mark.parametrize("interval", [5, "5"])
def test_replay_rejects_an_interval_that_is_not_a_timedelta(interval):
    """The comment lists this twice. An int and a string are the two non-timedelta inputs."""
    agent = FakeNodeAgent([])

    with pytest.raises(ValueError, match="interval is not a timedelta"):
        replay(agent, PAST, PAST + STEP, interval)


def test_replay_rejects_a_naive_start_or_end():
    agent = FakeNodeAgent([])
    naive = datetime(2020, 1, 1)

    with pytest.raises(TypeError):
        replay(agent, naive, PAST, STEP)
    with pytest.raises(TypeError):
        replay(agent, PAST, naive, STEP)


def _sample(
    instant: datetime,
    cpu_percent: float = 0.0,
    vm_state: VmState = VmState.RUNNING,
) -> VmSample:
    return VmSample(
        timestamp=instant,
        cpu_percent=cpu_percent,
        network_rx_bytes_delta=0,
        network_tx_bytes_delta=0,
        vm_state=vm_state,
    )
