"""Tests for the pause decision. Classification is passed in. Nothing calls a VM."""

from datetime import datetime, timedelta, timezone

from control.decider import decide
from control.types import Activity, AgentEvent, Decision, EventType, ObservationWindow, Policy, VmSample, VmState


UTC = timezone.utc
T0 = datetime(2026, 11, 16, 15, 0, 0, tzinfo=UTC)
POLICY = Policy()


def test_working_stays_running_through_a_long_quiet_period() -> None:
    window = _window(_quiet_samples(start=0, end=40))

    decision, reason = decide(Activity.WORKING, window, POLICY)

    assert decision is Decision.KEEP_RUNNING
    assert reason


def test_unknown_does_not_pause() -> None:
    window = _window(_quiet_samples(start=0, end=40))

    decision, _reason = decide(Activity.UNKNOWN, window, POLICY)

    assert decision is Decision.KEEP_RUNNING


def test_waiting_with_no_samples_keeps_running() -> None:
    decision, _reason = decide(Activity.WAITING, _window([]), POLICY)

    assert decision is Decision.KEEP_RUNNING


def test_waiting_shorter_than_the_quiet_period_keeps_running() -> None:
    window = _window(_quiet_samples(start=0, end=25))

    decision, _reason = decide(Activity.WAITING, window, POLICY)

    assert decision is Decision.KEEP_RUNNING


def test_waiting_for_the_quiet_period_pauses() -> None:
    window = _window(_quiet_samples(start=0, end=30))

    decision, _reason = decide(Activity.WAITING, window, POLICY)

    assert decision is Decision.PAUSE


def test_idle_for_the_quiet_period_pauses() -> None:
    window = _window(_quiet_samples(start=0, end=30))

    decision, _reason = decide(Activity.IDLE, window, POLICY)

    assert decision is Decision.PAUSE


def test_cpu_at_the_limit_is_not_quiet() -> None:
    """Policy max is 2%. A sample at 2% does not qualify."""
    samples = _quiet_samples(start=0, end=30)
    samples[-1] = _sample(30, cpu_percent=2.0)
    window = _window(samples)

    decision, _reason = decide(Activity.WAITING, window, POLICY)

    assert decision is Decision.KEEP_RUNNING


def test_received_bytes_during_the_quiet_period_keep_running() -> None:
    samples = _quiet_samples(start=0, end=30)
    samples[-1] = _sample(30, rx=1)

    decision, _reason = decide(Activity.IDLE, _window(samples), POLICY)

    assert decision is Decision.KEEP_RUNNING


def test_sent_bytes_during_the_quiet_period_keep_running() -> None:
    samples = _quiet_samples(start=0, end=30)
    samples[-1] = _sample(30, tx=1)

    decision, _reason = decide(Activity.IDLE, _window(samples), POLICY)

    assert decision is Decision.KEEP_RUNNING


def test_a_busy_sample_before_the_quiet_run_does_not_block_pause() -> None:
    samples = [_sample(0, cpu_percent=40.0, rx=100, tx=50), *_quiet_samples(start=10, end=40)]

    decision, _reason = decide(Activity.WAITING, _window(samples), POLICY)

    assert decision is Decision.PAUSE


def test_a_busy_sample_inside_the_quiet_run_keeps_running() -> None:
    samples = _quiet_samples(start=0, end=40)
    samples[2] = _sample(10, cpu_percent=9.0)

    decision, _reason = decide(Activity.WAITING, _window(samples), POLICY)

    assert decision is Decision.KEEP_RUNNING


def test_a_shorter_policy_quiet_period_can_pause_sooner() -> None:
    policy = Policy(quiet_period=timedelta(seconds=10))
    window = _window(_quiet_samples(start=0, end=10))

    decision, _reason = decide(Activity.WAITING, window, policy)

    assert decision is Decision.PAUSE


def test_a_higher_cpu_limit_treats_five_percent_as_quiet() -> None:
    policy = Policy(max_cpu_percent=10.0)
    samples = [_sample(0, cpu_percent=5.0), _sample(30, cpu_percent=5.0)]

    decision, _reason = decide(Activity.IDLE, _window(samples), policy)

    assert decision is Decision.PAUSE


def test_new_user_request_after_pause_resumes() -> None:
    paused_at = T0 + timedelta(seconds=30)
    samples = [_sample(0), _sample(30, vm_state=VmState.PAUSED)]
    events = [_event("evt_user", paused_at + timedelta(seconds=1), EventType.USER_INSTRUCTION)]
    window = _window(samples, events)

    decision, _reason = decide(Activity.WORKING, window, POLICY)

    assert decision is Decision.RESUME


def test_user_request_from_before_the_pause_does_not_resume() -> None:
    samples = [_sample(0, vm_state=VmState.PAUSED), _sample(30, vm_state=VmState.PAUSED)]
    events = [_event("evt_user", T0 - timedelta(seconds=5), EventType.USER_INSTRUCTION)]
    window = _window(samples, events)

    decision, _reason = decide(Activity.WAITING, window, POLICY)

    assert decision is Decision.KEEP_RUNNING


def test_a_user_request_does_not_resume_a_running_sandbox() -> None:
    samples = _quiet_samples(start=0, end=30)
    events = [_event("evt_user", T0 + timedelta(seconds=31), EventType.USER_INSTRUCTION)]
    window = _window(samples, events)

    decision, _reason = decide(Activity.WORKING, window, POLICY)

    assert decision is Decision.KEEP_RUNNING


def test_already_paused_without_a_new_request_does_not_pause_again() -> None:
    samples = [_sample(0, vm_state=VmState.PAUSED), _sample(30, vm_state=VmState.PAUSED)]

    decision, _reason = decide(Activity.WAITING, _window(samples), POLICY)

    assert decision is Decision.KEEP_RUNNING


def test_resume_wins_over_a_quiet_waiting_period() -> None:
    paused_at = T0 + timedelta(seconds=30)
    samples = [*_quiet_samples(start=0, end=30)]
    samples[-1] = _sample(30, vm_state=VmState.PAUSED)
    events = [_event("evt_user", paused_at + timedelta(seconds=1), EventType.USER_INSTRUCTION)]

    decision, _reason = decide(Activity.WAITING, _window(samples, events), POLICY)

    assert decision is Decision.RESUME


def _window(samples: list[VmSample], events: list[AgentEvent] | None = None) -> ObservationWindow:
    as_of: datetime = samples[-1].timestamp if samples else T0
    return ObservationWindow(as_of=as_of, events=events or [], samples=samples)


def _quiet_samples(start: int, end: int) -> list[VmSample]:
    samples: list[VmSample] = []
    second: int = start
    while second <= end:
        samples.append(_sample(second))
        second += 5
    return samples


def _sample(
    offset_seconds: int,
    cpu_percent: float = 0.1,
    rx: int = 0,
    tx: int = 0,
    vm_state: VmState = VmState.RUNNING,
) -> VmSample:
    return VmSample(
        timestamp=T0 + timedelta(seconds=offset_seconds),
        cpu_percent=cpu_percent,
        network_rx_bytes_delta=rx,
        network_tx_bytes_delta=tx,
        vm_state=vm_state,
    )


def _event(event_id: str, instant: datetime, event_type: EventType) -> AgentEvent:
    return AgentEvent(
        event_id=event_id,
        timestamp=instant,
        event_type=event_type,
        data={"message": "continue"},
    )
