import json
import uuid

import pytest

from chamber_ctl.data.calibration import CalibrationProfile, CalibrationRepository
from chamber_ctl.subsystems.euv_acquisition_controller import (
    CALIBRATION_PROVENANCE_RESOURCE,
    CAPTURE_SESSION_RESOURCE,
    resolve_exposure_calibration,
    write_capture_provenance,
)
from ipi_ecs.db.db_library import Library
from chamber_ctl.subsystems.exposure_controller import ExposureSettings
from chamber_ctl.data.dose_analysis import DoseAnalysisResult
from ipi_ecs.subsystems.experiment_controller import ExperimentReader, RunState
from euv_acquisition.timing import LaserTimingState


def _profile():
    return CalibrationProfile(
        profile_id=uuid.uuid4(),
        revision=1,
        name="Acquisition profile",
        created_at=1.0,
        algorithm_version="dose-v1",
        signal_polarity=1,
        load_resistance_ohms=50.0,
        photodiode_responsivity_a_per_w=0.14,
        illuminated_area_cm2=0.05,
    )


def test_exposure_calibration_resolution_requires_explicit_existing_revision(tmp_path) -> None:
    repository = CalibrationRepository(tmp_path)
    profile = _profile()
    repository.create(profile)
    repository.close()
    settings = ExposureSettings(
        calibration_profile_id=str(profile.profile_id),
        calibration_revision=1,
    )

    assert resolve_exposure_calibration(settings, tmp_path) == profile
    with pytest.raises(ValueError, match="requires a calibration profile"):
        resolve_exposure_calibration(ExposureSettings(), tmp_path)
    with pytest.raises(ValueError, match="was not found"):
        resolve_exposure_calibration(
            ExposureSettings(calibration_profile_id=str(profile.profile_id), calibration_revision=2),
            tmp_path,
        )


def test_acquisition_transmission_check_fails_closed_until_timing_status_is_fresh() -> None:
    from chamber_ctl.subsystems.euv_acquisition_controller import EuvAcquisitionSubsystem

    subsystem = object.__new__(EuvAcquisitionSubsystem)
    subsystem._timing_status_lock = __import__("threading").Lock()
    subsystem._timing_status = None
    subsystem._timing_status_received_at = 0.0

    assert subsystem._is_laser_transmitting() is False
    subsystem._on_timing_status(LaserTimingState(True, False, True, False, 10.0, 0.0, 10.0, 192.0).encode())
    assert subsystem._is_laser_transmitting() is True


def test_acquisition_timing_stream_records_initial_values_transitions_and_closure() -> None:
    from chamber_ctl.data.acquisition_runtime import LiveDoseAccumulator
    from chamber_ctl.subsystems.euv_acquisition_controller import EuvAcquisitionSubsystem, _AcquisitionRun

    class _Emitter:
        def __init__(self) -> None:
            self.events = []

        def emit(self, event) -> None:
            self.events.append(event)

        def flush(self, _timeout: float) -> bool:
            return True

    subsystem = object.__new__(EuvAcquisitionSubsystem)
    subsystem._run_lock = __import__("threading").RLock()
    subsystem._timing_status_lock = __import__("threading").Lock()
    subsystem._timing_status = LaserTimingState(True, False, True, False, 10.0, 0.0, 10.0, 192.0, 100, 200)
    subsystem._timing_status_received_at = 0.0
    subsystem._run_event_emitter = _Emitter()
    subsystem._log = lambda *_args, **_kwargs: None
    run = _AcquisitionRun(
        uuid.uuid4(),
        _profile(),
        None,
        None,
        192.0,
        LiveDoseAccumulator(_profile()),
        session_id=uuid.uuid4(),
    )
    subsystem._run = run

    subsystem._open_timing_event_stream(run)
    subsystem._on_timing_status(LaserTimingState(True, False, True, False, 10.0, 0.0, 10.0, 192.0, 101, 201).encode())
    subsystem._on_timing_status(LaserTimingState(True, False, True, False, 0.0, 0.0, 10.0, 192.0, 102, 202).encode())
    assert subsystem._close_timing_event_stream(run, outcome="STOPPED") is True

    events = subsystem._run_event_emitter.events
    assert [event.kind for event in events] == [
        "stream.start",
        "timing.triggers_enabled",
        "timing.euv_transmitting",
        "timing.euv_transmitting",
        "stream.end",
    ]
    assert [event.sequence for event in events] == list(range(len(events)))
    assert events[1].payload["value"] is True
    assert events[2].payload["value"] is True
    assert events[3].payload["value"] is False
    assert events[3].payload["timing_state"]["sampled_at_unix_ns"] == 102


def test_graph_generation_failure_does_not_block_acquisition_finalization(monkeypatch, tmp_path) -> None:
    import chamber_ctl.subsystems.euv_acquisition_controller as acquisition_controller
    from chamber_ctl.subsystems.euv_acquisition_controller import EuvAcquisitionSubsystem

    messages = []
    subsystem = object.__new__(EuvAcquisitionSubsystem)
    subsystem._data_path = tmp_path
    subsystem._log = lambda message, **fields: messages.append((message, fields))
    monkeypatch.setattr(
        acquisition_controller,
        "ensure_exposure_graph",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("disk full")),
    )

    subsystem._ensure_persisted_exposure_graph(uuid.uuid4(), object(), context="test finalization")

    assert messages[0][1]["event"] == "exposure_graph_generation_failed"
    assert "disk full" in messages[0][0]


def test_can_start_rejects_an_unreleased_digitizer_session_before_preinit(monkeypatch) -> None:
    import chamber_ctl.subsystems.euv_acquisition_controller as acquisition_controller
    from euv_acquisition.session import CaptureSessionManifest, CaptureSessionState

    orphaned_session = CaptureSessionManifest(
        server_boot_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        state=CaptureSessionState.ORPHANED,
        source_kind="simulated",
        source_id="fixture",
        started_at_unix_ns=1,
        stop_reason="Digitizer service restarted during capture.",
    )

    class _Client:
        instances = []

        def __init__(self, _control_address, _artifact_address):
            self.connected = False
            self.closed = False
            self.__class__.instances.append(self)

        def connect(self):
            self.connected = True

        def command(self, command):
            assert command == "status"
            return {"session": orphaned_session.to_dict()}

        def close(self):
            self.closed = True

    monkeypatch.setattr(acquisition_controller, "AcquisitionClient", _Client)
    monkeypatch.setattr(acquisition_controller, "resolve_exposure_calibration", lambda *_args: _profile())
    subsystem = object.__new__(acquisition_controller.EuvAcquisitionSubsystem)
    subsystem._run_lock = __import__("threading").RLock()
    subsystem._run = None
    subsystem._capture_client = None
    subsystem._artifact_importer = None
    subsystem._temporary_directory = None
    subsystem._next_capture_connect_monotonic = 0.0
    subsystem._data_path = "unused"
    subsystem._log = lambda *_args, **_kwargs: None
    state = RunState("exposure", ExposureSettings(target_dose=1.0, chopper_frequency_hz=192.0), s_uuid=uuid.uuid4())

    allowed, reason = subsystem._can_start(state.get_settings(), state)

    assert allowed is False
    assert b"orphaned session" in reason
    assert subsystem._run is None
    assert _Client.instances[0].connected is True
    assert _Client.instances[0].closed is False

    with pytest.raises(RuntimeError, match="orphaned session"):
        subsystem._check_capture_service_ready()
    assert len(_Client.instances) == 1


def test_capture_provenance_is_persisted_before_snapshot_import(tmp_path) -> None:
    profile = _profile()
    library = Library(tmp_path)
    entry = library.create_entry("Exposure", "Fixture")
    session_id = uuid.uuid4()
    try:
        write_capture_provenance(entry, session_id, profile, 192.0)
        assert entry.get_tags()["euv_capture_session_id"] == str(session_id)
        with entry.resource(CALIBRATION_PROVENANCE_RESOURCE, "euv_calibration_profile", "r") as resource:
            assert json.load(resource)["content_hash"] == profile.content_hash
        with entry.resource(CAPTURE_SESSION_RESOURCE, "euv_capture_session", "r") as resource:
            assert json.load(resource)["session_id"] == str(session_id)
    finally:
        library.close()


def test_rejected_preinit_run_clears_without_waiting_for_a_capture_client() -> None:
    from chamber_ctl.subsystems.euv_acquisition_controller import EuvAcquisitionSubsystem, _AcquisitionRun

    subsystem = object.__new__(EuvAcquisitionSubsystem)
    subsystem._run_lock = __import__("threading").RLock()
    subsystem._capture_client = None
    subsystem._run = _AcquisitionRun(uuid.uuid4(), _profile(), None, None, 192.0, object())
    stopped = []
    subsystem._on_did_stop = stopped.append
    subsystem._log = lambda *_args, **_kwargs: None

    subsystem._on_stop(None)
    subsystem._run.stop_requested_monotonic = 0.0
    subsystem._advance_finalization()

    assert subsystem._run is None
    assert stopped == [b"EUV acquisition cleared a rejected start before capture opened."]


def test_capture_command_logs_send_completion_and_failure() -> None:
    from chamber_ctl.subsystems.euv_acquisition_controller import EuvAcquisitionSubsystem

    class _Client:
        def command(self, command, payload):
            if command == "fail":
                raise TimeoutError("response timeout")
            return {"command": command, "payload": payload}

    subsystem = object.__new__(EuvAcquisitionSubsystem)
    subsystem._capture_client = _Client()
    subsystem._next_capture_connect_monotonic = 0.0
    logs = []
    subsystem._log = lambda message, **kwargs: logs.append((message, kwargs))

    assert subsystem._capture_command("status", {}) == {"command": "status", "payload": {}}
    with pytest.raises(TimeoutError, match="response timeout"):
        subsystem._capture_command("fail", {})

    assert [item[1]["event"] for item in logs] == [
        "digitizer_command_sent",
        "digitizer_command_completed",
        "digitizer_command_sent",
        "digitizer_command_failed",
        "digitizer_connection_lost",
    ]


def test_snapshot_import_keeps_other_pending_snapshot_report_totals() -> None:
    from chamber_ctl.subsystems.euv_acquisition_controller import EuvAcquisitionSubsystem, _AcquisitionRun

    first_snapshot_id = uuid.uuid4()

    class _Manifest:
        def __init__(self, snapshot_id, final_sequence):
            self.snapshot_id = snapshot_id
            self.final_sequence = final_sequence

    class _Importer:
        def import_snapshot(self, _record, manifest, *, before_ack):
            return __import__("pathlib").Path(f"C:/imported/{manifest.snapshot_id}.h5")

    subsystem = object.__new__(EuvAcquisitionSubsystem)
    subsystem._run_lock = __import__("threading").RLock()
    run = _AcquisitionRun(uuid.uuid4(), _profile(), None, None, 192.0, object())
    run.report_totals = {10: (1.0, 0.1), 20: (2.0, 0.2)}
    run.pending_snapshots = {first_snapshot_id: _Manifest(first_snapshot_id, 10)}
    subsystem._run = run
    subsystem._artifact_importer = _Importer()
    subsystem._get_record = lambda _run_id: object()
    subsystem._log = lambda *_args, **_kwargs: None
    subsystem._segment_publisher = None

    subsystem._import_snapshot(run.pending_snapshots[first_snapshot_id])

    assert 10 not in run.report_totals
    assert run.report_totals[20] == (2.0, 0.2)


def test_finalizing_snapshot_import_failure_raises_for_deferred_recovery() -> None:
    from chamber_ctl.subsystems.euv_acquisition_controller import EuvAcquisitionSubsystem, _AcquisitionRun

    snapshot_id = uuid.uuid4()

    class _Manifest:
        def __init__(self, value):
            self.final_sequence = 1
            self.snapshot_id = value

    subsystem = object.__new__(EuvAcquisitionSubsystem)
    subsystem._run_lock = __import__("threading").RLock()
    run = _AcquisitionRun(uuid.uuid4(), _profile(), None, None, 192.0, object(), finalizing=True)
    run.report_totals = {1: (1.0, 0.1)}
    run.pending_snapshots = {snapshot_id: _Manifest(snapshot_id)}
    subsystem._run = run
    subsystem._import_snapshot = lambda _manifest: (_ for _ in ()).throw(RuntimeError("artifact write failed"))

    with pytest.raises(RuntimeError, match="Could not reconcile snapshot"):
        subsystem._import_pending_snapshots()


def test_finalization_renews_the_stop_event_before_controller_timeout() -> None:
    from chamber_ctl.subsystems.euv_acquisition_controller import EuvAcquisitionSubsystem, _AcquisitionRun

    feedback = []
    subsystem = object.__new__(EuvAcquisitionSubsystem)
    subsystem._last_stop_feedback_monotonic = 0.0
    subsystem._on_stop_feedback = feedback.append
    subsystem._log = lambda *_args, **_kwargs: None
    run = _AcquisitionRun(uuid.uuid4(), _profile(), None, None, 192.0, object())
    run.finalization_phase = "waiting for pulse reports"
    run.finalization_detail = "Waiting to reconcile 1 snapshot."

    subsystem._keep_stop_event_alive(run)

    assert feedback == [b"EUV acquisition waiting for pulse reports: Waiting to reconcile 1 snapshot."]


def test_worker_finalization_error_defers_recovery_without_terminating_the_worker() -> None:
    from chamber_ctl.subsystems.euv_acquisition_controller import EuvAcquisitionSubsystem, _AcquisitionRun

    subsystem = object.__new__(EuvAcquisitionSubsystem)
    subsystem._run_lock = __import__("threading").RLock()
    run = _AcquisitionRun(uuid.uuid4(), _profile(), None, None, 192.0, object(), finalizing=True)
    subsystem._run = run
    failures = []
    deferred = []
    subsystem._handle_exception = failures.append
    subsystem._defer_finalization = lambda current_run, detail: deferred.append((current_run, detail))

    subsystem._handle_worker_failure(ValueError("timeline dose regressed"))

    assert failures and isinstance(failures[0], ValueError)
    assert deferred == [(run, "ValueError: timeline dose regressed")]


def test_deferred_finalization_stops_an_active_capture_before_recovery() -> None:
    from chamber_ctl.subsystems.euv_acquisition_controller import EuvAcquisitionSubsystem, _AcquisitionRun

    class _Client:
        def __init__(self) -> None:
            self.commands = []

        def command(self, command, payload):
            self.commands.append((command, payload))
            if command == "status":
                return {"capture_active": True}
            return {}

    subsystem = object.__new__(EuvAcquisitionSubsystem)
    subsystem._run = None
    subsystem._capture_client = _Client()
    subsystem._deferred_finalization_detail = None
    subsystem._log = lambda *_args, **_kwargs: None
    subsystem._on_did_stop = lambda _reason: None
    run = _AcquisitionRun(uuid.uuid4(), _profile(), None, None, 192.0, object(), session_id=uuid.uuid4())

    subsystem._defer_finalization(run, "timeline write failed")

    assert subsystem._capture_client.commands == [
        ("status", {}),
        ("stop_capture", {"reason": "Exposure finalization deferred; retaining artifacts for recovery."}),
    ]


def test_final_analysis_tags_survive_a_stale_terminal_record_write(tmp_path) -> None:
    from chamber_ctl.subsystems.euv_acquisition_controller import EuvAcquisitionSubsystem, _AcquisitionRun

    run_id = uuid.uuid4()
    library = Library(tmp_path)
    entry = library.create_entry("Exposure", "Fixture")
    entry.set_tag("experiment", "exposure")
    entry.set_tag("run", run_id.hex)
    with entry.resource("run.json", "run_state", "w") as resource:
        resource.write(RunState("exposure", ExposureSettings(target_dose=1.0), s_uuid=run_id).encode())
    with entry.resource("metadata.json", "metadata", "w") as resource:
        resource.write('{"created_at":1.0,"version":1}')

    stale_controller_entry = entry
    result = DoseAnalysisResult(run_id, _profile(), (), runtime_seconds=2.5)
    run = _AcquisitionRun(run_id, _profile(), None, None, 192.0, object())
    subsystem = object.__new__(EuvAcquisitionSubsystem)
    subsystem._reader = ExperimentReader(str(tmp_path), "exposure")
    subsystem._log = lambda *_args, **_kwargs: None
    try:
        subsystem._promote_final_analysis(run, result)
        stale_controller_entry.set_tag("status", "STOPPED")
        tags = library.read_entry(entry.get_uuid()).get_tags()

        assert tags["status"] == "STOPPED"
        assert float(tags["dose"]) == 0.0
        assert float(tags["runtime"]) == 2.5
        assert "active_dose_analysis" in tags
    finally:
        subsystem._reader.close()
        library.close()