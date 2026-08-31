import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from chamber_ctl.data.calibration import CalibrationProfile, CalibrationRepository
from chamber_ctl.data.capture_cadence import decode_live_cadence
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


def test_acquisition_status_publishes_live_dose_and_zero_runtime() -> None:
    import threading

    from chamber_ctl.data.acquisition_runtime import LiveDoseAccumulator
    from chamber_ctl.subsystems.euv_acquisition_controller import EuvAcquisitionSubsystem, _AcquisitionRun

    class _Publisher:
        def __init__(self) -> None:
            self.value = None

    accumulator = LiveDoseAccumulator(_profile())
    accumulator.accumulated_dose_mj_cm2 = 2.5
    accumulator.transmitting_runtime_seconds = 0.0
    run = _AcquisitionRun(uuid.uuid4(), _profile(), None, None, 192.0, accumulator)
    subsystem = object.__new__(EuvAcquisitionSubsystem)
    subsystem._last_publish = float("-inf")
    subsystem._run_lock = threading.RLock()
    subsystem._timing_status_lock = threading.Lock()
    subsystem._timing_status = None
    subsystem._run = run
    subsystem._diagnostic = None
    subsystem._board_status = {}
    subsystem._capture_client = object()
    subsystem._deferred_finalization_detail = None
    subsystem._diagnostic_error = None
    subsystem._last_diagnostic_summary = None
    subsystem._dose_publisher = _Publisher()
    subsystem._time_publisher = _Publisher()
    subsystem._status_publisher = _Publisher()
    subsystem._health_publisher = _Publisher()
    subsystem._cadence_publisher = _Publisher()

    subsystem._publish_values()

    status = json.loads(subsystem._status_publisher.value)
    cadence = decode_live_cadence(subsystem._cadence_publisher.value)
    assert status["accumulated_dose_mj_cm2"] == 2.5
    assert status["transmitting_runtime_seconds"] == 0.0
    assert cadence.context == "exposure"
    assert cadence.run_id == run.run_id


def test_completed_diagnostic_retains_transferred_snapshot_summary() -> None:
    import threading

    from chamber_ctl.subsystems.euv_acquisition_controller import EuvAcquisitionSubsystem, _DiagnosticCapture

    diagnostic = _DiagnosticCapture(uuid.uuid4(), "one_shot", "simulated", "fixture", 0.0, report_count=1)
    diagnostic.processed_snapshot_ids.add(uuid.uuid4())
    subsystem = object.__new__(EuvAcquisitionSubsystem)
    subsystem._run_lock = threading.RLock()
    subsystem._diagnostic = diagnostic
    subsystem._diagnostic_error = None
    subsystem._last_diagnostic_summary = None
    subsystem._log = lambda *_args, **_kwargs: None

    subsystem._complete_diagnostic()

    assert subsystem._last_diagnostic_summary == {
        "mode": "one_shot",
        "report_count": 1,
        "snapshot_count": 1,
    }


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
        def import_snapshot(self, _record, manifest, *, before_ack, after_persist):
            path = __import__("pathlib").Path(f"C:/imported/{manifest.snapshot_id}.h5")
            after_persist(_record, path)
            return path

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
    subsystem._preview_publisher = None

    subsystem._import_snapshot(run.pending_snapshots[first_snapshot_id])

    assert 10 not in run.report_totals
    assert run.report_totals[20] == (2.0, 0.2)


def test_diagnostic_snapshot_is_previewed_acknowledged_purged_then_deleted(monkeypatch, tmp_path) -> None:
    import chamber_ctl.subsystems.euv_acquisition_controller as acquisition_controller
    from chamber_ctl.subsystems.euv_acquisition_controller import EuvAcquisitionSubsystem, _DiagnosticCapture

    events = []
    session_id = uuid.uuid4()
    snapshot_id = uuid.uuid4()
    manifest = SimpleNamespace(session_id=session_id, snapshot_id=snapshot_id, filename="diagnostic.h5")
    local_path = tmp_path / manifest.filename

    class _Importer:
        def fetch_verified_snapshot(self, value):
            assert value is manifest
            local_path.write_bytes(b"verified")
            events.append("fetch")
            return local_path

        def acknowledge_snapshot(self, value):
            assert value is manifest
            assert local_path.is_file()
            events.append("acknowledge")

    class _Preview:
        def encode(self):
            events.append("encode")
            return b"preview"

    class _Publisher:
        @property
        def value(self):
            return None

        @value.setter
        def value(self, payload):
            assert payload == b"preview"
            assert local_path.is_file()
            events.append("publish")

    monkeypatch.setattr(
        acquisition_controller,
        "build_acquisition_preview",
        lambda path, value, **kwargs: (
            events.append("preview") or _Preview()
            if path == local_path and value is manifest and kwargs == {"context": "diagnostic"}
            else None
        ),
    )
    subsystem = object.__new__(EuvAcquisitionSubsystem)
    subsystem._run_lock = __import__("threading").RLock()
    subsystem._diagnostic = _DiagnosticCapture(session_id, "continuous", "simulated", "fixture", 0.0)
    subsystem._diagnostic.pending_snapshots[snapshot_id] = manifest
    subsystem._artifact_importer = _Importer()
    subsystem._preview_publisher = _Publisher()
    subsystem._temporary_directory = Path(tmp_path)
    subsystem._log = lambda *_args, **_kwargs: None

    def capture_command(command, payload):
        assert command == "purge_snapshot"
        assert payload == {"snapshot_id": str(snapshot_id)}
        assert local_path.is_file()
        assert events[-1] == "acknowledge"
        events.append("purge")
        return {"purged": True}

    subsystem._capture_command = capture_command

    subsystem._import_diagnostic_snapshot(manifest)

    assert events == ["fetch", "preview", "encode", "publish", "acknowledge", "purge"]
    assert not local_path.exists()
    assert subsystem._diagnostic.processed_snapshot_ids == {snapshot_id}
    assert subsystem._diagnostic.pending_snapshots == {}


def test_one_shot_diagnostic_stops_discards_and_completes_original_handle() -> None:
    from chamber_ctl.subsystems.euv_acquisition_controller import EuvAcquisitionSubsystem, _DiagnosticCapture
    from euv_acquisition.session import CapturePurpose, CaptureSessionManifest, CaptureSessionState

    class _Handle:
        def __init__(self):
            self.returned = []
            self.failed = []

        def ret(self, value):
            self.returned.append(value)

        def fail(self, value):
            self.failed.append(value)

    session_id = uuid.uuid4()
    handle = _Handle()
    diagnostic = _DiagnosticCapture(session_id, "one_shot", "simulated", "fixture", 0.0, report_count=1)
    diagnostic.completion_handles.append(handle)
    terminal = CaptureSessionManifest(
        server_boot_id=uuid.uuid4(),
        session_id=session_id,
        state=CaptureSessionState.STOPPED,
        source_kind="simulated",
        source_id="fixture",
        started_at_unix_ns=1,
        purpose=CapturePurpose.DIAGNOSTIC,
        final_sequence=0,
        stop_reason="one shot",
    )
    commands = []
    subsystem = object.__new__(EuvAcquisitionSubsystem)
    subsystem._run_lock = __import__("threading").RLock()
    subsystem._diagnostic = diagnostic
    subsystem._log = lambda *_args, **_kwargs: None

    def capture_command(command, payload):
        commands.append((command, payload))
        if command == "stop_capture":
            return {"session": terminal.to_dict()}
        if command == "list_snapshots":
            return {"snapshots": []}
        if command == "discard_diagnostic_session":
            return {"discarded": True}
        raise AssertionError(command)

    subsystem._capture_command = capture_command

    subsystem._advance_diagnostic()

    assert [command for command, _payload in commands] == [
        "stop_capture",
        "list_snapshots",
        "discard_diagnostic_session",
    ]
    assert subsystem._diagnostic is None
    assert handle.failed == []
    assert b"1 pulse report" in handle.returned[0]


def test_diagnostic_start_never_discards_a_retained_experiment_session() -> None:
    from chamber_ctl.subsystems.euv_acquisition_controller import EuvAcquisitionSubsystem
    from euv_acquisition.session import CaptureSessionManifest, CaptureSessionState

    retained = CaptureSessionManifest(
        server_boot_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        state=CaptureSessionState.STOPPED,
        source_kind="simulated",
        source_id="fixture",
        started_at_unix_ns=1,
        stop_reason="exposure stopped",
    )
    commands = []
    subsystem = object.__new__(EuvAcquisitionSubsystem)
    subsystem._run_lock = __import__("threading").RLock()
    subsystem._run = None
    subsystem._diagnostic = None
    subsystem._diagnostic_start_pending = True
    subsystem._ensure_capture_client = lambda **_kwargs: object()
    subsystem._log = lambda *_args, **_kwargs: None

    def capture_command(command, payload):
        commands.append((command, payload))
        return {
            "capabilities": {
                "capture_purpose": True,
                "purge_snapshot": True,
                "discard_diagnostic_session": True,
            },
            "session": retained.to_dict(),
        }

    subsystem._capture_command = capture_command

    with pytest.raises(RuntimeError, match="retains experiment session"):
        subsystem._start_diagnostic("continuous", object())

    assert commands == [("status", {})]


def test_simulator_control_handler_validates_json_before_queueing() -> None:
    from chamber_ctl.subsystems.euv_acquisition_controller import EuvAcquisitionSubsystem

    class _Handle:
        def __init__(self) -> None:
            self.failed = []
            self.feedback_values = []

        def fail(self, value):
            self.failed.append(value)

        def feedback(self, value):
            self.feedback_values.append(value)

    subsystem = object.__new__(EuvAcquisitionSubsystem)
    subsystem._control_requests = __import__("queue").Queue()
    valid = _Handle()
    invalid = _Handle()

    subsystem._on_set_simulator_control(
        None,
        b'{"name":"pll_locked","enabled":false}',
        valid,
    )
    subsystem._on_set_simulator_control(
        None,
        b'{"name":"physical_laser","enabled":true}',
        invalid,
    )

    request = subsystem._control_requests.get_nowait()
    assert request.action == "simulator_set"
    assert request.payload == {"name": "pll_locked", "enabled": False}
    assert valid.failed == []
    assert valid.feedback_values
    assert b"Unknown simulator control" in invalid.failed[0]
    assert subsystem._control_requests.empty()


def test_simulator_controls_fail_closed_without_board_capability() -> None:
    from chamber_ctl.subsystems.euv_acquisition_controller import EuvAcquisitionSubsystem

    commands = []
    subsystem = object.__new__(EuvAcquisitionSubsystem)

    def capture_command(command, payload):
        commands.append((command, payload))
        return {
            "source_kind": "hardware",
            "capabilities": {"simulator_controls": False},
        }

    subsystem._capture_command = capture_command

    with pytest.raises(RuntimeError, match="simulator_controls"):
        subsystem._set_simulator_control({"name": "pll_locked", "enabled": False})

    assert commands == [("status", {})]


def test_stop_resumes_failed_diagnostic_only_after_purpose_check() -> None:
    from chamber_ctl.subsystems.euv_acquisition_controller import EuvAcquisitionSubsystem, _DiagnosticCapture
    from euv_acquisition.session import CapturePurpose, CaptureSessionManifest, CaptureSessionState

    session_id = uuid.uuid4()
    session = CaptureSessionManifest(
        server_boot_id=uuid.uuid4(),
        session_id=session_id,
        state=CaptureSessionState.STOPPED,
        source_kind="simulated",
        source_id="fixture",
        started_at_unix_ns=1,
        purpose=CapturePurpose.DIAGNOSTIC,
        final_sequence=0,
        stop_reason="transport failed",
    )
    subsystem = object.__new__(EuvAcquisitionSubsystem)
    subsystem._run_lock = __import__("threading").RLock()
    subsystem._capture_client = object()
    subsystem._diagnostic = _DiagnosticCapture(
        session_id,
        "continuous",
        "simulated",
        "fixture",
        0.0,
        state="error",
        terminal_error="preview failed",
    )
    commands = []
    subsystem._capture_command = lambda command, payload: (
        commands.append((command, payload))
        or {"capture_active": False, "session": session.to_dict()}
    )

    subsystem._resume_diagnostic_stop("operator stop")

    assert commands == [("status", {})]
    assert subsystem._diagnostic.state == "finalizing"
    assert subsystem._diagnostic.stop_sent is True


def test_connection_loss_marks_a_running_diagnostic_failed() -> None:
    from chamber_ctl.subsystems.euv_acquisition_controller import EuvAcquisitionSubsystem, _DiagnosticCapture

    class _Client:
        def heartbeat_if_due(self):
            raise ConnectionError("link down")

    subsystem = object.__new__(EuvAcquisitionSubsystem)
    subsystem._run_lock = __import__("threading").RLock()
    subsystem._run = None
    subsystem._diagnostic = _DiagnosticCapture(uuid.uuid4(), "continuous", "simulated", "fixture", 0.0)
    subsystem._capture_client = _Client()
    subsystem._discard_capture_client = lambda _reason: setattr(subsystem, "_capture_client", None)
    failures = []
    subsystem._fail_diagnostic = failures.append

    subsystem._maintain_capture_connection()

    assert subsystem._capture_client is None
    assert len(failures) == 1
    assert "ConnectionError: link down" in failures[0]


def test_errored_diagnostic_does_not_retry_queued_imports() -> None:
    from chamber_ctl.subsystems.euv_acquisition_controller import EuvAcquisitionSubsystem, _DiagnosticCapture

    class _Client:
        def get_report(self, timeout=0):
            raise AssertionError("errored diagnostics must pause queue consumption")

    subsystem = object.__new__(EuvAcquisitionSubsystem)
    subsystem._run_lock = __import__("threading").RLock()
    subsystem._run = None
    subsystem._diagnostic = _DiagnosticCapture(
        uuid.uuid4(),
        "continuous",
        "simulated",
        "fixture",
        0.0,
        state="error",
        terminal_error="preview failed",
    )
    subsystem._capture_client = _Client()

    subsystem._consume_capture_events()


def test_unsolicited_digitizer_stop_fails_diagnostic_with_original_reason() -> None:
    from chamber_ctl.subsystems.euv_acquisition_controller import EuvAcquisitionSubsystem, _DiagnosticCapture

    class _Client:
        def __init__(self) -> None:
            self.reason_sent = False

        def get_report(self, timeout=0):
            raise __import__("queue").Empty

        def get_snapshot(self, timeout=0):
            raise __import__("queue").Empty

        def get_stop_reason(self, timeout=0):
            if self.reason_sent:
                raise __import__("queue").Empty
            self.reason_sent = True
            return "Pulse clipping threshold exceeded."

    subsystem = object.__new__(EuvAcquisitionSubsystem)
    subsystem._run_lock = __import__("threading").RLock()
    subsystem._diagnostic = _DiagnosticCapture(uuid.uuid4(), "continuous", "simulated", "fixture", 0.0)
    subsystem._import_pending_diagnostic_snapshots = lambda: None

    subsystem._consume_diagnostic_events(_Client())

    assert subsystem._diagnostic.state == "finalizing"
    assert subsystem._diagnostic.stop_reason == "Pulse clipping threshold exceeded."
    assert "Pulse clipping threshold exceeded" in subsystem._diagnostic.terminal_error


def test_ambiguous_diagnostic_start_remains_owned_and_is_stopped_after_reconnect() -> None:
    from chamber_ctl.subsystems.euv_acquisition_controller import EuvAcquisitionSubsystem
    from euv_acquisition.session import CapturePurpose, CaptureSessionManifest, CaptureSessionState

    subsystem = object.__new__(EuvAcquisitionSubsystem)
    subsystem._run_lock = __import__("threading").RLock()
    subsystem._run = None
    subsystem._diagnostic = None
    subsystem._diagnostic_start_pending = True
    subsystem._diagnostic_error = None
    subsystem._ensure_capture_client = lambda **_kwargs: object()
    subsystem._log = lambda *_args, **_kwargs: None
    commands = []

    def start_command(command, payload):
        commands.append((command, payload))
        if command == "status":
            return {
                "source_kind": "simulated",
                "source_id": "fixture",
                "capabilities": {
                    "capture_purpose": True,
                    "purge_snapshot": True,
                    "discard_diagnostic_session": True,
                },
                "session": None,
            }
        raise TimeoutError("response lost")

    subsystem._capture_command = start_command

    with pytest.raises(TimeoutError, match="response lost"):
        subsystem._start_diagnostic("continuous", object())

    diagnostic = subsystem._diagnostic
    assert diagnostic is not None
    assert diagnostic.state == "error"
    assert diagnostic.session_id == uuid.UUID(commands[1][1]["session_id"])
    active = CaptureSessionManifest(
        server_boot_id=uuid.uuid4(),
        session_id=diagnostic.session_id,
        state=CaptureSessionState.ACTIVE,
        source_kind="simulated",
        source_id="fixture",
        started_at_unix_ns=1,
        purpose=CapturePurpose.DIAGNOSTIC,
    )
    stopped = CaptureSessionManifest(
        server_boot_id=active.server_boot_id,
        session_id=active.session_id,
        state=CaptureSessionState.STOPPED,
        source_kind="simulated",
        source_id="fixture",
        started_at_unix_ns=1,
        purpose=CapturePurpose.DIAGNOSTIC,
        stop_reason="stopped after uncertain start",
    )

    def reconcile_command(command, payload):
        commands.append((command, payload))
        if command == "status":
            return {"capture_active": True, "session": active.to_dict()}
        if command == "stop_capture":
            return {"capture_active": False, "session": stopped.to_dict()}
        raise AssertionError(command)

    subsystem._capture_command = reconcile_command
    subsystem._reconcile_failed_diagnostic_stop()

    assert [command for command, _payload in commands[-2:]] == ["status", "stop_capture"]
    assert subsystem._diagnostic.stop_sent is True
    assert subsystem._diagnostic.state == "error"


def test_connect_cleanup_discards_only_an_unowned_persisted_diagnostic() -> None:
    from chamber_ctl.subsystems.euv_acquisition_controller import EuvAcquisitionSubsystem
    from euv_acquisition.session import CapturePurpose, CaptureSessionManifest, CaptureSessionState

    diagnostic = CaptureSessionManifest(
        server_boot_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        state=CaptureSessionState.ACTIVE,
        source_kind="simulated",
        source_id="fixture",
        started_at_unix_ns=1,
        purpose=CapturePurpose.DIAGNOSTIC,
    )
    subsystem = object.__new__(EuvAcquisitionSubsystem)
    subsystem._run_lock = __import__("threading").RLock()
    subsystem._run = None
    subsystem._diagnostic = None
    subsystem._diagnostic_start_pending = False
    subsystem._log = lambda *_args, **_kwargs: None
    discarded = []
    subsystem._discard_retained_diagnostic = lambda session, *, capture_active: discarded.append(
        (session, capture_active)
    )
    subsystem._capture_command = lambda command, payload: {
        "source_kind": "simulated",
        "capabilities": {"discard_diagnostic_session": True},
        "session": None,
    }
    subsystem._cache_board_status = lambda _status: None
    status = {
        "capture_active": True,
        "capabilities": {"discard_diagnostic_session": True},
        "session": diagnostic.to_dict(),
    }

    subsystem._cleanup_stale_diagnostic_after_connect(status)

    assert discarded == [(diagnostic, True)]

    experiment = CaptureSessionManifest(
        server_boot_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        state=CaptureSessionState.STOPPED,
        source_kind="hardware",
        source_id="fixture",
        started_at_unix_ns=1,
        stop_reason="exposure stopped",
    )
    subsystem._cleanup_stale_diagnostic_after_connect(status | {"session": experiment.to_dict()})
    assert discarded == [(diagnostic, True)]


def test_acknowledged_diagnostic_snapshot_is_not_refetched_after_uncertain_purge() -> None:
    from chamber_ctl.subsystems.euv_acquisition_controller import EuvAcquisitionSubsystem, _DiagnosticCapture
    from euv_acquisition.session import StoredSnapshot
    from euv_acquisition.models import SnapshotCloseReason
    from euv_acquisition.snapshot import SnapshotManifest

    session_id = uuid.uuid4()
    snapshot_id = uuid.uuid4()
    manifest = SnapshotManifest(
        snapshot_id=snapshot_id,
        session_id=session_id,
        filename=f"snap_{snapshot_id}.h5",
        byte_count=1,
        sha256="0" * 64,
        pulse_count=1,
        first_sequence=0,
        final_sequence=0,
        first_capture_unix_ns=1,
        final_capture_unix_ns=1,
        close_reason=SnapshotCloseReason.EXPLICIT_FLUSH,
    )
    diagnostic = _DiagnosticCapture(session_id, "continuous", "simulated", "fixture", 0.0)
    diagnostic.pending_snapshots[snapshot_id] = manifest

    EuvAcquisitionSubsystem._merge_diagnostic_snapshots(
        diagnostic,
        (StoredSnapshot(manifest, acknowledged=True),),
    )

    assert diagnostic.pending_snapshots == {}
    assert diagnostic.processed_snapshot_ids == {snapshot_id}


def test_ambiguous_diagnostic_stop_enters_owned_error_reconciliation() -> None:
    from chamber_ctl.subsystems.euv_acquisition_controller import EuvAcquisitionSubsystem, _DiagnosticCapture

    subsystem = object.__new__(EuvAcquisitionSubsystem)
    subsystem._run_lock = __import__("threading").RLock()
    subsystem._diagnostic = _DiagnosticCapture(uuid.uuid4(), "continuous", "simulated", "fixture", 0.0)
    subsystem._diagnostic_error = None
    subsystem._log = lambda *_args, **_kwargs: None
    subsystem._capture_command = lambda _command, _payload: (_ for _ in ()).throw(
        TimeoutError("response lost")
    )

    with pytest.raises(TimeoutError, match="response lost"):
        subsystem._stop_diagnostic("operator stop")

    assert subsystem._diagnostic.state == "error"
    assert subsystem._diagnostic.stop_sent is False
    assert "stop outcome is unknown" in subsystem._diagnostic.terminal_error


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