from dataclasses import replace
from types import SimpleNamespace
import queue
import uuid

import pytest

from test_acquisition_interlock_freshness import fixture
from chamber_ctl.subsystems import euv_acquisition_controller as controller


def setup(monkeypatch):
    owner, run, capture, clock = fixture(monkeypatch)
    logs, completed = [], []
    owner._log = lambda message, **details: logs.append((message, details))
    owner._on_did_stop = completed.append
    owner._capture_command = lambda command, payload: {'capture_active': False, 'snapshots': []}
    owner._deferred_finalization_detail = None
    owner._ensure_persisted_exposure_graph = lambda *_args, **_kwargs: None
    return owner, run, clock, logs, completed


def send(owner, *, on=True, phase=10.0, rate=190.0, timestamp=1):
    state = replace(owner._timing_status, laser_on=on, current_phase=phase,
                    chopper_frequency_hz=rate, sampled_at_unix_ns=timestamp,
                    sampled_at_monotonic_ns=timestamp)
    owner._on_timing_status(state.encode())
    return state


def overflow(owner):
    owner._timing_observations = queue.Queue(maxsize=1)
    send(owner, on=True)
    send(owner, on=False)


def test_exposure_heartbeats_and_measurement_noise_do_not_fill_transition_queue(monkeypatch):
    owner, run, clock, _logs, _completed = setup(monkeypatch)
    for n in range(5000):
        clock[0] += .1
        send(owner, phase=10 + (n % 2) * .001, rate=190 + (n % 7) * .01, timestamp=n)
    assert owner._timing_observations.qsize() == 1
    assert owner._timing_observation_error is None
    assert owner._timing_status.sampled_at_monotonic_ns == 4999
    assert owner._timing_status_received_at == clock[0]
    assert run.timing_observation_fault is None


def test_transition_timestamps_and_first_valid_timestamp_are_preserved(monkeypatch):
    owner, run, _clock, _logs, _completed = setup(monkeypatch)
    run.timing_stream = object()
    observed = []
    owner._record_timing_state = lambda _run, state: observed.append(state)
    expected = [send(owner, timestamp=None), send(owner, timestamp=10)]
    send(owner, timestamp=11)
    expected.extend([send(owner, on=False, timestamp=12), send(owner, on=True, timestamp=13),
                     send(owner, phase=0, timestamp=14), send(owner, phase=10, timestamp=15)])
    owner._consume_timing_observations()
    assert observed == expected


def test_diagnostic_rate_changes_remain_semantic_transitions(monkeypatch):
    owner, _run, _clock, _logs, _completed = setup(monkeypatch)
    owner._run = None
    rates = []
    owner._diagnostic = SimpleNamespace(session_id=uuid.uuid4(), state='running',
        cadence=SimpleNamespace(set_expected_rate=rates.append))
    send(owner, rate=190)
    send(owner, rate=190, timestamp=2)
    send(owner, rate=192, timestamp=3)
    owner._consume_timing_observations()
    assert rates == [95, 96]


def test_true_overflow_detaches_full_queue_and_fault_does_not_cross_sessions(monkeypatch):
    owner, run, _clock, _logs, _completed = setup(monkeypatch)
    overflow(owner)
    failed_queue = owner._timing_observations
    with pytest.raises(RuntimeError, match='timing history is incomplete'):
        owner._consume_timing_observations()
    assert owner._timing_observations is not failed_queue
    assert owner._timing_observations.empty()
    assert owner._timing_observation_error is None
    # Even if the transport recovers, the affected session stays incomplete.
    with pytest.raises(RuntimeError, match='timing history is incomplete'):
        owner._consume_timing_observations()
    run.session_id = uuid.uuid4()
    send(owner, on=False, timestamp=3)
    owner._consume_timing_observations()
    assert owner._timing_observations.empty()


def test_repeated_overflow_during_deferred_stop_cannot_crash_cleanup(monkeypatch):
    owner, run, _clock, logs, completed = setup(monkeypatch)
    overflow(owner)
    with pytest.raises(RuntimeError) as original:
        owner._consume_timing_observations()
    def command(name, payload):
        if name == 'status':
            # The screenshot's reentrant overflow while a stop RPC is pending.
            send(owner, on=True, timestamp=4)
            send(owner, on=False, timestamp=5)
        return {'capture_active': False}
    owner._capture_command = command
    owner._defer_finalization(run, str(original.value))
    assert owner._run is None
    assert run.timing_stream_closed
    assert owner._timing_observation_error is None
    assert len(completed) == 1
    assert 'timing history is incomplete' in owner._deferred_finalization_detail
    assert sum(details.get('event') == 'timing_observation_overflow' for _, details in logs) == 2
    owner._consume_timing_observations()  # The worker can keep running in recovery-required state.


def test_stream_close_freezes_input_but_drains_queued_final_transition(monkeypatch):
    owner, run, _clock, _logs, _completed = setup(monkeypatch)
    observed, events = [], []
    run.timing_stream = SimpleNamespace(event=lambda kind, payload, **kwargs: (kind, payload))
    owner._record_timing_state = lambda _run, state: observed.append(state)
    def emit(event):
        events.append(event)
        # Timing updates still arrive while the stream-end is persisted.
        for n in range(2000):
            send(owner, on=bool(n % 2), timestamp=100+n)
    owner._run_event_emitter = SimpleNamespace(emit=emit, flush=lambda _timeout: True)
    first, last = send(owner, on=True, timestamp=1), send(owner, on=False, timestamp=2)
    assert owner._close_timing_event_stream(run, outcome='STOPPED')
    assert observed == [first, last]
    assert events[-1][1]['outcome'] == 'STOPPED'
    assert run.timing_stream_closed and run.timing_observations_frozen
    assert owner._timing_observations.empty()
    assert owner._timing_observation_error is None
    assert owner._timing_status.sampled_at_monotonic_ns == 2099


def prepare_finalization(owner, run, clock):
    run.finalizing = True
    run.stop_requested_monotonic = clock[0] - 10
    run.stop_capture_sent = True
    run.stop_deadline_monotonic = clock[0] - 5
    owner._last_stop_feedback_monotonic = clock[0]
    owner._get_record = lambda _run_id: SimpleNamespace(get_record=lambda: object())
    promoted = []
    owner._promote_final_analysis = lambda *_args: promoted.append(True)
    return promoted


def test_finalization_never_analyzes_promotes_or_releases_incomplete_timing(monkeypatch):
    owner, run, clock, _logs, completed = setup(monkeypatch)
    promoted = prepare_finalization(owner, run, clock)
    analyzed = []
    monkeypatch.setattr(controller, 'analyze_experiment_entry', lambda *_args, **_kwargs: analyzed.append(True))
    overflow(owner)
    owner._advance_finalization()
    assert not analyzed and not promoted
    assert not run.release_pending
    assert owner._run is None
    assert len(completed) == 1
    assert 'timing history is incomplete' in owner._deferred_finalization_detail


def test_final_analysis_receives_frozen_timing_and_late_fault_still_blocks_promotion(monkeypatch):
    owner, run, clock, _logs, completed = setup(monkeypatch)
    promoted = prepare_finalization(owner, run, clock)
    def analyze(*args, **kwargs):
        assert run.timing_observations_frozen and run.timing_stream_closed
        for n in range(2000):
            send(owner, on=bool(n % 2), timestamp=100+n)
        assert owner._timing_observation_error is None
        run.timing_observation_fault = (run.session_id, 'injected late timing fault')
        return SimpleNamespace(total_dose_mj_cm2=1, runtime_seconds=1)
    monkeypatch.setattr(controller, 'analyze_experiment_entry', analyze)
    owner._advance_finalization()
    assert not promoted and not run.release_pending
    assert owner._run is None and len(completed) == 1
    assert owner._deferred_finalization_detail.endswith('injected late timing fault')


def test_cleanup_emission_failure_preserves_primary_error_and_stop_ack(monkeypatch):
    owner, run, _clock, logs, completed = setup(monkeypatch)
    run.timing_stream = SimpleNamespace(event=lambda *args, **kwargs: None)
    owner._run_event_emitter = SimpleNamespace(emit=lambda _event: (_ for _ in ()).throw(OSError('stream unavailable')))
    owner._defer_finalization(run, 'original timing loss')
    assert owner._deferred_finalization_detail == 'original timing loss'
    assert owner._run is None and len(completed) == 1
    assert any(details.get('event') == 'acquisition_deferred_timing_close_failed' for _, details in logs)
