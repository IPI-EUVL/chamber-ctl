import json
import queue
import threading
import uuid
from types import SimpleNamespace

import pytest

from chamber_ctl.data.acquisition_runtime import LiveDoseAccumulator
from chamber_ctl.data.calibration import CalibrationProfile
from chamber_ctl.subsystems import euv_acquisition_controller as controller
from euv_acquisition.health import AcquisitionHealth
from euv_acquisition.models import NativePulseAnalysis, PulseQuality, PulseReport
from euv_acquisition.service import ReportActivity
from euv_acquisition.timing import LaserTimingState


def fixture(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(controller.time, "monotonic", lambda: clock[0])
    profile = CalibrationProfile(uuid.uuid4(), 1, "Fixture", 1.0, "dose-v1", 1, 50.0, 0.14, 0.05)
    accumulator = LiveDoseAccumulator(profile)
    accumulator.set_running(True)
    accumulator.dose_rate_mj_cm2_s = 1.0
    run = controller._AcquisitionRun(uuid.uuid4(), profile, None, None, 190.0, accumulator)
    run.session_id = uuid.uuid4()
    run.last_sequence = 12
    run.last_pulse_monotonic = 99.2
    # HEAD and the inherited work-in-progress use different startup-grace anchors.
    run.running_started_monotonic = 90.0
    run.transmitting_started_monotonic = 90.0
    capture = SimpleNamespace(activity=ReportActivity(run.session_id, 88, 99.99))
    capture.get_report_activity = lambda _session: capture.activity
    owner = object.__new__(controller.EuvAcquisitionSubsystem)
    owner._run_lock = threading.RLock()
    owner._run = run
    owner._diagnostic = None
    owner._last_publish = float("-inf")
    owner._last_health_payload = AcquisitionHealth(False, None, None, None, False, False, False).encode()
    owner._last_health_context = None
    owner._pending_health_loss = None
    owner._timing_status_lock = threading.Lock()
    owner._timing_status = LaserTimingState(True, False, True, False, 10.0, 0.0, 10.0, 190.0)
    owner._timing_status_received_at = 100.0
    owner._timing_observations = queue.Queue(maxsize=1024)
    owner._timing_observation_error = None
    owner._board_status = {}
    owner._capture_client = capture
    owner._deferred_finalization_detail = None
    owner._diagnostic_error = None
    owner._last_diagnostic_summary = None
    owner._dose_publisher = SimpleNamespace(value=None)
    owner._time_publisher = SimpleNamespace(value=None)
    owner._status_publisher = SimpleNamespace(value=None)
    owner._health_publisher = SimpleNamespace(value=None)
    owner._cadence_publisher = None
    return owner, run, capture, clock


def publish(owner):
    owner._last_publish = float("-inf")
    owner._publish_values()
    return AcquisitionHealth.decode(owner._health_publisher.value)


def hold_run_lock(owner):
    acquired, release = threading.Event(), threading.Event()

    def hold():
        with owner._run_lock:
            acquired.set()
            release.wait(3)

    thread = threading.Thread(target=hold)
    thread.start()
    assert acquired.wait(1)
    return release, thread


def test_healthy_receipts_prevent_false_loss_after_slow_import(monkeypatch):
    owner, run, _capture, _clock = fixture(monkeypatch)
    health = publish(owner)
    status = json.loads(owner._status_publisher.value)
    assert not health.pulse_loss
    assert not run.pulse_loss_active
    assert health.last_sequence == 12
    assert health.last_pulse_age_seconds == pytest.approx(0.01)
    assert status["last_received_sequence"] == 88
    assert status["last_report_received_age_seconds"] == pytest.approx(0.01)
    assert status["last_pulse_age_seconds"] == pytest.approx(0.8)
    assert status["dose_rate_mj_cm2_s"] is None


@pytest.mark.parametrize("receipt", ["silence", "foreign", "none"])
def test_true_receipt_loss_still_trips_at_250ms(monkeypatch, receipt):
    owner, run, capture, _clock = fixture(monkeypatch)
    capture.activity = {
        "silence": ReportActivity(run.session_id, 88, 99.749),
        "foreign": ReportActivity(uuid.uuid4(), 1000, 100.0),
        "none": None,
    }[receipt]
    assert publish(owner).pulse_loss
    assert run.pulse_loss_active
    assert not run.resume_authorized


def test_lock_busy_publication_detects_and_retains_real_loss(monkeypatch):
    owner, run, capture, clock = fixture(monkeypatch)
    assert not publish(owner).pulse_loss
    release, thread = hold_run_lock(owner)
    try:
        clock[0] = 100.3
        assert publish(owner).pulse_loss
        capture.activity = ReportActivity(run.session_id, 89, clock[0])
        assert publish(owner).pulse_loss  # pending alarm survives new reports
    finally:
        release.set()
        thread.join(1)
    assert not publish(owner).pulse_loss  # stream recovered, authorization has not
    assert run.pulse_loss_active
    assert not run.recovery_ready
    assert not run.resume_authorized
    assert run.consecutive_timed_pulses == 0


def test_each_loss_episode_requires_new_stable_recovery_reports(monkeypatch):
    owner, run, capture, clock = fixture(monkeypatch)
    assert not publish(owner).pulse_loss
    clock[0] += 0.3
    run.consecutive_timed_pulses = 1000
    assert publish(owner).pulse_loss
    assert run.consecutive_timed_pulses == 0

    capture.activity = ReportActivity(run.session_id, 100, clock[0])
    assert not publish(owner).pulse_loss
    run.consecutive_timed_pulses = 10
    run.recovery_ready = True
    run.resume_authorized = True
    assert run.pulse_loss_active  # historical fault flag remains intact

    clock[0] += 0.3
    assert publish(owner).pulse_loss
    assert run.consecutive_timed_pulses == 0
    assert not run.recovery_ready and not run.resume_authorized

    # Captured timestamps can be contiguous across a network/reporting pause.
    run.last_report_monotonic_ns = 1_000_000_000
    report = PulseReport(
        run.session_id, 101, 2_000_000_000, 1_010_526_315,
        NativePulseAnalysis(0.0, 0.0, 0.0, 0.1, 0.1, PulseQuality.OK, "native-v1"),
    )
    owner._ingest_report(report)
    assert run.consecutive_timed_pulses == 1
    assert not run.recovery_ready and not run.resume_authorized


def test_receipts_remain_healthy_during_and_after_import_lock(monkeypatch):
    owner, run, capture, clock = fixture(monkeypatch)
    assert not publish(owner).pulse_loss
    release, thread = hold_run_lock(owner)
    try:
        for index in range(1, 5):
            clock[0] += 0.6
            capture.activity = ReportActivity(run.session_id, 88 + index, clock[0] - 0.01)
            assert not publish(owner).pulse_loss
    finally:
        release.set()
        thread.join(1)
    assert not publish(owner).pulse_loss
    assert not run.pulse_loss_active
    assert json.loads(owner._status_publisher.value)["last_pulse_age_seconds"] > 3


@pytest.mark.parametrize("already_lost", [False, True])
def test_timing_off_during_import_does_not_create_or_erase_alarm(monkeypatch, already_lost):
    owner, run, _capture, clock = fixture(monkeypatch)
    assert not publish(owner).pulse_loss
    release, thread = hold_run_lock(owner)
    try:
        clock[0] += 0.3
        if already_lost:
            assert publish(owner).pulse_loss
        owner._on_timing_status(LaserTimingState(False, False, True, False, 10.0, 0.0, 10.0, 190.0).encode())
        assert publish(owner).pulse_loss is already_lost
    finally:
        release.set()
        thread.join(1)
    publish(owner)
    assert run.pulse_loss_active is already_lost


def test_startup_grace_expires_even_while_import_lock_is_busy(monkeypatch):
    owner, run, capture, clock = fixture(monkeypatch)
    run.last_pulse_monotonic = None
    run.running_started_monotonic = clock[0]
    run.transmitting_started_monotonic = clock[0]
    capture.activity = None
    assert not publish(owner).pulse_loss
    release, thread = hold_run_lock(owner)
    try:
        clock[0] += 5.01
        assert publish(owner).pulse_loss
    finally:
        release.set()
        thread.join(1)


def test_pending_health_fault_cannot_cross_session_replacement_on_same_run(monkeypatch):
    owner, run, capture, clock = fixture(monkeypatch)
    assert not publish(owner).pulse_loss
    previous_session = run.session_id
    release, thread = hold_run_lock(owner)
    try:
        clock[0] += 0.3
        assert publish(owner).pulse_loss
        assert owner._pending_health_loss[0] is run
        assert owner._pending_health_loss[1] == previous_session
        # Model a session replacement by the owner of the contended state lock.
        run.session_id = uuid.uuid4()
        capture.activity = ReportActivity(run.session_id, 1, clock[0])
        health = publish(owner)
        assert not health.pulse_loss
        assert health.session_id is None  # old cached context is not republished
    finally:
        release.set()
        thread.join(1)
    health = publish(owner)
    assert health.session_id == run.session_id
    assert not health.pulse_loss
    assert not run.pulse_loss_active
    assert owner._pending_health_loss is None
    clock[0] += 0.3
    assert publish(owner).pulse_loss  # the replacement session is still monitored


def test_timing_callback_does_not_wait_for_import_and_preserves_transitions(monkeypatch):
    owner, run, _capture, _clock = fixture(monkeypatch)
    run.timing_stream, run.timing_stream_closed = object(), False
    observed = []
    owner._record_timing_state = lambda active, state: observed.append((active, state))
    states = [LaserTimingState(True, False, True, False, phase, 0.0, 10.0, 190.0, index, index)
              for index, phase in enumerate((10.0, 0.0, 10.0), start=1)]
    release, holder = hold_run_lock(owner)
    finished = threading.Event()

    def callbacks():
        for state in states:
            owner._on_timing_status(state.encode())
        finished.set()

    callback = threading.Thread(target=callbacks)
    callback.start()
    try:
        assert finished.wait(0.5), "DDS callback waited for snapshot-import lock"
        assert observed == []
        assert owner._timing_status == states[-1]
    finally:
        release.set()
        holder.join(1)
        callback.join(1)
    owner._consume_timing_observations()
    assert observed == [(run, state) for state in states]


def test_timing_queue_rejects_old_session_and_reports_overflow(monkeypatch):
    owner, run, _capture, _clock = fixture(monkeypatch)
    run.timing_stream, run.timing_stream_closed = object(), False
    observed = []
    owner._record_timing_state = lambda active, state: observed.append(state)
    owner._on_timing_status(owner._timing_status.encode())
    run.session_id = uuid.uuid4()
    owner._consume_timing_observations()
    assert observed == []
    owner._timing_observations = queue.Queue(maxsize=1)
    owner._on_timing_status(owner._timing_status.encode())
    owner._on_timing_status(owner._timing_status.encode())
    with pytest.raises(RuntimeError, match="timing observation queue overflowed"):
        owner._consume_timing_observations()
