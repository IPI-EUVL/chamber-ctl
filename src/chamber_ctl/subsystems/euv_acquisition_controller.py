from __future__ import annotations

import os
import json
import queue
import shutil
import tempfile
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import ipi_ecs.core.tcp as tcp
import ipi_ecs.dds.client as client
import ipi_ecs.dds.subsystem as dds_subsystem
import ipi_ecs.dds.types as types
from ipi_ecs.core.daemon import Daemon, StopFlag
from ipi_ecs.logging.client import LogClient
import ipi_ecs.subsystems.experiment_client as exp_client
from ipi_ecs.subsystems.experiment_controller import ExperimentReader, RunState
from ipi_ecs.subsystems.run_events import (
    STREAM_END_KIND,
    STREAM_START_KIND,
    RunEventEmitter,
    RunEventStream,
    run_event_stream_id,
)
import segment_bytes

from chamber_ctl.data.acquisition_artifacts import AcquisitionArtifactImporter
from chamber_ctl.data.acquisition_preview import build_acquisition_preview
from chamber_ctl.data.acquisition_runtime import LiveDoseAccumulator, PulseSequenceGap
from chamber_ctl.data.calibration import CalibrationProfile, CalibrationRepository
from chamber_ctl.data.dose_analysis import (
    CaptureTimelinePoint,
    DoseAnalysisResult,
    DoseAnalysisRevision,
    analyze_experiment_entry,
    append_capture_timeline_point,
    write_analysis_revision,
)
from chamber_ctl.data.exposure_graph import ensure_exposure_graph
from chamber_ctl.subsystems import uuids
from chamber_ctl.subsystems.exposure_controller import ExposureSettings
from euv_acquisition.timing import LaserTimingState

from euv_acquisition.models import PulseQuality, PulseReport
from euv_acquisition.health import AcquisitionHealth
from euv_acquisition.service import AcquisitionClient
from euv_acquisition.session import CapturePurpose, CaptureSessionManifest, CaptureSessionState, StoredSnapshot


CALIBRATION_PROVENANCE_RESOURCE = "euv_calibration_profile.json"
CAPTURE_SESSION_RESOURCE = "euv_capture_session.json"
SIMULATOR_CONTROL_NAMES = frozenset({"laser_enabled", "chopper_enabled", "pll_locked"})


def write_capture_provenance(entry, session_id: uuid.UUID, calibration: CalibrationProfile, chopper_frequency_hz: float) -> None:
    if chopper_frequency_hz <= 0:
        raise ValueError("chopper_frequency_hz must be positive.")
    entry.set_tag("euv_capture_session_id", str(session_id))
    with entry.resource(CALIBRATION_PROVENANCE_RESOURCE, "euv_calibration_profile", "w") as resource:
        json.dump(calibration.to_dict(), resource, allow_nan=False, separators=(",", ":"))
    with entry.resource(CAPTURE_SESSION_RESOURCE, "euv_capture_session", "w") as resource:
        json.dump(
            {
                "session_id": str(session_id),
                "calibration_profile_id": str(calibration.profile_id),
                "calibration_revision": calibration.revision,
                "calibration_hash": calibration.content_hash,
                "chopper_frequency_hz": chopper_frequency_hz,
            },
            resource,
            allow_nan=False,
            separators=(",", ":"),
        )


def resolve_exposure_calibration(settings: ExposureSettings, data_path: str | Path) -> CalibrationProfile:
    profile_id_text = settings.get_calibration_profile_id()
    revision = settings.get_calibration_revision()
    if not isinstance(profile_id_text, str) or not profile_id_text.strip():
        raise ValueError("Exposure requires a calibration profile ID.")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ValueError("Exposure requires a positive calibration revision.")
    try:
        profile_id = uuid.UUID(profile_id_text)
    except ValueError as exc:
        raise ValueError("Exposure calibration profile ID is not a UUID.") from exc
    repository = CalibrationRepository(data_path)
    try:
        profile = repository.get(profile_id, revision)
    finally:
        repository.close()
    if profile is None:
        raise ValueError(f"Calibration profile {profile_id} revision {revision} was not found.")
    return profile


@dataclass
class _AcquisitionRun:
    run_id: uuid.UUID
    calibration: CalibrationProfile
    target_dose: float | None
    target_time: float | None
    chopper_frequency_hz: float
    accumulator: LiveDoseAccumulator
    session_id: uuid.UUID | None = None
    imported_snapshot_ids: set[uuid.UUID] = field(default_factory=set)
    finalizing: bool = False
    stop_requested_monotonic: float = 0.0
    stop_capture_sent: bool = False
    stop_deadline_monotonic: float = 0.0
    finalization_phase: str | None = None
    finalization_detail: str | None = None
    release_pending: bool = False
    last_pulse_monotonic: float | None = None
    running_started_monotonic: float | None = None
    last_report_monotonic_ns: int | None = None
    last_sequence: int | None = None
    consecutive_timed_pulses: int = 0
    pulse_loss_active: bool = False
    recovery_ready: bool = False
    resume_authorized: bool = False
    report_totals: dict[int, tuple[float, float]] = field(default_factory=dict)
    pending_snapshots: dict[uuid.UUID, object] = field(default_factory=dict)
    source_kind: str | None = None
    source_id: str | None = None
    clipped_pulse_count: int = 0
    last_pulse_clipped: bool = False
    timing_stream: RunEventStream | None = None
    timing_initial_emitted: bool = False
    timing_stream_closed: bool = False
    last_timing_status: LaserTimingState | None = None


@dataclass(frozen=True)
class _AcquisitionControlRequest:
    action: str
    payload: dict
    handle: object


@dataclass
class _DiagnosticCapture:
    session_id: uuid.UUID
    mode: str
    source_kind: str
    source_id: str
    started_monotonic: float
    state: str = "running"
    report_count: int = 0
    last_sequence: int | None = None
    pending_snapshots: dict[uuid.UUID, object] = field(default_factory=dict)
    processed_snapshot_ids: set[uuid.UUID] = field(default_factory=set)
    stop_sent: bool = False
    stop_reason: str | None = None
    terminal_error: str | None = None
    completion_handles: list[object] = field(default_factory=list)
    next_cleanup_attempt_monotonic: float = 0.0


class EuvAcquisitionSubsystem(exp_client.ExperimentClient):
    STATUS_UPDATE_SECONDS = 0.1
    STOP_QUIET_SECONDS = 0.5
    STOP_DRAIN_CAP_SECONDS = 5.0
    STOP_ACK_DELAY_SECONDS = 0.05
    STOP_FEEDBACK_INTERVAL_SECONDS = 5.0
    CAPTURE_RECONNECT_DELAY_SECONDS = 2.0
    ONE_SHOT_TIMEOUT_SECONDS = 15.0

    def __init__(self, data_path: str | None = None) -> None:
        self._data_path = data_path or os.path.join(os.environ["EUVL_PATH"], "datasets")
        self._run_lock = threading.RLock()
        self._run: _AcquisitionRun | None = None
        self._diagnostic: _DiagnosticCapture | None = None
        self._diagnostic_start_pending = False
        self._diagnostic_error: str | None = None
        self._last_diagnostic_summary: dict | None = None
        self._control_requests: queue.Queue[_AcquisitionControlRequest] = queue.Queue()
        self._recovery_active = False
        self._capture_client: AcquisitionClient | None = None
        self._artifact_importer: AcquisitionArtifactImporter | None = None
        self._temporary_directory: Path | None = None
        self._next_capture_connect_monotonic = 0.0
        self._board_status: dict = {}
        self._reader: ExperimentReader | None = None
        self._preinit_pending = False
        self._start_pending = False
        self._recovery_requests: queue.Queue = queue.Queue()
        self._deferred_finalization_detail: str | None = None
        self._last_publish = 0.0
        self._last_stop_feedback_monotonic = 0.0
        self._stop_requested = False
        self._timing_status = None
        self._timing_status_received_at = 0.0
        self._timing_status_lock = threading.Lock()
        self._subsystem = None
        self._dose_publisher = None
        self._time_publisher = None
        self._segment_publisher = None
        self._preview_publisher = None
        self._status_publisher = None
        self._health_publisher = None
        self._timing_status_kv = None
        self._run_event_provider = None
        self._run_event_emitter = RunEventEmitter(uuids.UUID_EXPOSURE_CONTROLLER)
        self._stop_exposure_event = None
        self._did_configure = False

        client_uuid = uuid.uuid4()
        self._logger_socket = tcp.TCPClientSocket()
        self._logger_socket.connect(("127.0.0.1", 11751))
        self._logger_socket.start()
        self._logger = LogClient(self._logger_socket, origin_uuid=client_uuid)
        self._dds_client = client.DDSClient(client_uuid, logger=self._logger)
        self._dds_client.when_ready().then(self._on_dds_ready)

        super().__init__("exposure", "EUV Acquisition", self._logger)
        self.register_experiment_settings_type(ExposureSettings)
        self._daemon = Daemon(exception_handler=self._handle_exception)
        self._daemon.add(self._worker)
        self._daemon.start()

    def _handle_exception(self, exc: Exception) -> None:
        print(f"[EUV Acquisition] Worker exception: {type(exc).__name__}: {exc}", flush=True)
        self._log(f"Unhandled acquisition worker error: {exc}", level="ERROR")
        for line in traceback.format_exception(None, exc, exc.__traceback__):
            for part in line.splitlines():
                if part:
                    self._log(part, level="ERROR")

    def _log(self, message: str, level: str = "INFO", **data) -> None:
        print(f"[EUV Acquisition] {message}", flush=True)
        try:
            self._logger.log(message, level=level, l_type="ACQ", subsystem="EUV Acquisition", **data)
        except Exception as exc:
            print(f"[EUV Acquisition] Shared log delivery failed: {type(exc).__name__}: {exc}", flush=True)

    def _on_dds_ready(self) -> None:
        if self._did_configure:
            return
        self._did_configure = True
        handle = self._dds_client.register_subsystem("EUV Acquisition Controller", uuids.UUID_EUV_ACQUISITION_CONTROLLER)
        self._subsystem = handle
        self._dose_publisher = handle.get_kv_property(b"cur_dose", False, True, True)
        self._time_publisher = handle.get_kv_property(b"cur_time", False, True, True)
        self._segment_publisher = handle.get_kv_property(b"new_segment", False, True, True)
        self._preview_publisher = handle.get_kv_property(b"acquisition_preview", False, True, True)
        self._status_publisher = handle.get_kv_property(b"acquisition_status", False, True, True)
        self._health_publisher = handle.get_kv_property(b"acquisition_health", False, True, True)
        self._dose_publisher.set_type(types.FloatTypeSpecifier())
        self._time_publisher.set_type(types.FloatTypeSpecifier())
        self._preview_publisher.set_type(types.ByteTypeSpecifier())
        self._status_publisher.set_type(types.ByteTypeSpecifier())
        self._health_publisher.set_type(types.ByteTypeSpecifier())
        self._timing_status_kv = handle.add_remote_kv(
            uuids.UUID_LASER_CONTROLLER,
            dds_subsystem.KVDescriptor(types.ByteTypeSpecifier(), b"timing_status", True, True, False),
        )
        self._timing_status_kv.on_new_data_received(self._on_timing_status)
        self._run_event_provider = handle.add_event_provider(b"append_exposure_run_event")
        self._run_event_emitter.set_provider(self._run_event_provider)
        self._stop_exposure_event = handle.add_event_provider(b"stop_exposure")
        handle.add_event_handler(b"resume_acquisition_interlock").on_called(self._on_resume_interlock)
        handle.add_event_handler(b"recover_orphaned_capture_session").on_called(self._on_recover_orphaned_capture_session)
        handle.add_event_handler(b"acquisition_test_start").on_called(self._on_diagnostic_start)
        handle.add_event_handler(b"acquisition_test_one_shot").on_called(self._on_diagnostic_one_shot)
        handle.add_event_handler(b"acquisition_test_flush").on_called(self._on_diagnostic_flush)
        handle.add_event_handler(b"acquisition_test_stop").on_called(self._on_diagnostic_stop)
        handle.add_event_handler(b"set_acquisition_simulator_control").on_called(self._on_set_simulator_control)
        handle.add_event_handler(b"restore_acquisition_simulator_controls").on_called(self._on_restore_simulator_controls)
        self._setup_subsystem(handle)
        try:
            self._ensure_capture_client()
        except Exception:
            self._log(
                "Digitizer service is not yet reachable; persistent connection will retry.",
                level="WARNING",
                event="digitizer_connection_deferred",
            )

    def _on_resume_interlock(self, _sender_uuid, _payload, handle) -> None:
        with self._run_lock:
            run = self._run
            if run is None or not run.pulse_loss_active:
                self._log("Rejected interlock recovery authorization because no pulse-loss interlock is active.", level="WARNING", event="interlock_resume_rejected")
                handle.fail(b"No acquisition pulse-loss interlock is active.")
                return
            if not run.recovery_ready:
                self._log("Rejected interlock recovery authorization because stable recovery pulses are incomplete.", level="WARNING", event="interlock_resume_rejected", run_id=str(run.run_id))
                handle.fail(b"Acquisition has not received ten stable recovery pulses.")
                return
            run.resume_authorized = True
        self._log("Operator authorized acquisition interlock recovery.", event="interlock_resume_authorized", run_id=str(run.run_id))
        handle.ret(b"Acquisition interlock recovery authorized.")

    def _on_recover_orphaned_capture_session(self, _sender_uuid, payload, handle) -> None:
        self._log("Received orphaned capture recovery request.", event="orphan_recovery_requested")
        if payload != b"confirm":
            self._log("Rejected orphaned capture recovery without explicit confirmation.", level="WARNING", event="orphan_recovery_rejected")
            handle.fail(b"Orphaned capture recovery requires the explicit confirm payload.")
            return
        with self._run_lock:
            if self._run is not None:
                self._log("Rejected orphaned capture recovery while an acquisition run is active.", level="WARNING", event="orphan_recovery_rejected", run_id=str(self._run.run_id))
                handle.fail(b"Cannot recover an orphaned session while an acquisition run is active.")
                return
            if self._diagnostic is not None or self._diagnostic_start_pending:
                self._log("Rejected orphaned capture recovery while diagnostics are active.", level="WARNING", event="orphan_recovery_rejected")
                handle.fail(b"Cannot recover an orphaned session while acquisition diagnostics are active.")
                return
        self._log("Queued orphaned capture recovery for worker processing.", event="orphan_recovery_queued")
        self._recovery_requests.put(handle)
        handle.feedback(b"Orphaned capture recovery queued.")

    def _on_diagnostic_start(self, _sender_uuid, payload, handle) -> None:
        self._queue_diagnostic_start("continuous", payload, handle)

    def _on_diagnostic_one_shot(self, _sender_uuid, payload, handle) -> None:
        self._queue_diagnostic_start("one_shot", payload, handle)

    def _queue_diagnostic_start(self, mode: str, payload, handle) -> None:
        if payload != b"":
            handle.fail(b"Acquisition diagnostic start does not accept a payload.")
            return
        with self._run_lock:
            if self._run is not None:
                handle.fail(b"Acquisition diagnostics are idle-only and cannot run during an exposure.")
                return
            if self._diagnostic is not None or self._diagnostic_start_pending:
                handle.fail(b"An acquisition diagnostic is already active or starting.")
                return
            if self._deferred_finalization_detail is not None or self._recovery_active or not self._recovery_requests.empty():
                handle.fail(b"Acquisition diagnostics cannot start while artifact recovery is required or active.")
                return
            self._diagnostic_start_pending = True
        self._control_requests.put(_AcquisitionControlRequest("diagnostic_start", {"mode": mode}, handle))
        handle.feedback(f"Acquisition {mode.replace('_', '-')} diagnostic queued.".encode("utf-8"))

    def _on_diagnostic_flush(self, _sender_uuid, payload, handle) -> None:
        if payload != b"":
            handle.fail(b"Acquisition diagnostic flush does not accept a payload.")
            return
        with self._run_lock:
            diagnostic = self._diagnostic
            if diagnostic is None or diagnostic.mode != "continuous" or diagnostic.state != "running":
                handle.fail(b"A continuous acquisition diagnostic must be running before it can be flushed.")
                return
        self._control_requests.put(_AcquisitionControlRequest("diagnostic_flush", {}, handle))
        handle.feedback(b"Acquisition diagnostic flush queued.")

    def _on_diagnostic_stop(self, _sender_uuid, payload, handle) -> None:
        if payload != b"":
            handle.fail(b"Acquisition diagnostic stop does not accept a payload.")
            return
        with self._run_lock:
            if self._diagnostic is None:
                handle.fail(b"No acquisition diagnostic is active.")
                return
        self._control_requests.put(_AcquisitionControlRequest("diagnostic_stop", {}, handle))
        handle.feedback(b"Acquisition diagnostic stop queued.")

    def _on_set_simulator_control(self, _sender_uuid, payload, handle) -> None:
        try:
            value = json.loads(bytes(payload).decode("utf-8"))
            if not isinstance(value, dict) or set(value) != {"name", "enabled"}:
                raise ValueError("Simulator control payload requires only name and enabled.")
            if value["name"] not in SIMULATOR_CONTROL_NAMES:
                raise ValueError(f"Unknown simulator control {value['name']!r}.")
            if not isinstance(value["enabled"], bool):
                raise ValueError("Simulator control enabled value must be boolean.")
        except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            handle.fail(str(exc).encode("utf-8", errors="replace"))
            return
        self._control_requests.put(_AcquisitionControlRequest("simulator_set", value, handle))
        handle.feedback(b"Simulator control update queued.")

    def _on_restore_simulator_controls(self, _sender_uuid, payload, handle) -> None:
        if payload != b"":
            handle.fail(b"Simulator control restore does not accept a payload.")
            return
        self._control_requests.put(_AcquisitionControlRequest("simulator_restore", {}, handle))
        handle.feedback(b"Simulator control restore queued.")

    def _on_timing_status(self, payload: bytes) -> None:
        try:
            status = LaserTimingState.decode(payload)
        except ValueError:
            return
        with self._timing_status_lock:
            self._timing_status = status
            self._timing_status_received_at = time.monotonic()
        run_lock = getattr(self, "_run_lock", None)
        if run_lock is None:
            return
        with run_lock:
            run = self._run
            if run is not None and run.timing_stream is not None and not run.timing_stream_closed:
                self._record_timing_state(run, status)

    def _emit_timing_event(
        self,
        run: _AcquisitionRun,
        *,
        kind: str,
        value: bool,
        status: LaserTimingState,
    ) -> bool:
        if run.timing_stream is None:
            return False
        if status.sampled_at_unix_ns is None or status.sampled_at_monotonic_ns is None:
            return False
        event = run.timing_stream.event(
            kind,
            {
                "value": value,
                "timing_state": status.to_dict(),
                "timestamp_quality": "producer",
            },
            producer_unix_ns=status.sampled_at_unix_ns,
            producer_monotonic_ns=status.sampled_at_monotonic_ns,
            capture_session_id=run.session_id,
            next_sequence=None if run.last_sequence is None else run.last_sequence + 1,
            runtime_seconds=run.accumulator.transmitting_runtime_seconds,
        )
        self._run_event_emitter.emit(event)
        return True

    def _record_timing_state(self, run: _AcquisitionRun, status: LaserTimingState) -> None:
        previous = run.last_timing_status
        if not run.timing_initial_emitted:
            emitted_trigger = self._emit_timing_event(
                run,
                kind="timing.triggers_enabled",
                value=status.triggers_enabled,
                status=status,
            )
            emitted_transmitting = self._emit_timing_event(
                run,
                kind="timing.euv_transmitting",
                value=status.euv_transmitting(),
                status=status,
            )
            run.timing_initial_emitted = emitted_trigger and emitted_transmitting
        elif previous is not None:
            if previous.triggers_enabled != status.triggers_enabled:
                self._emit_timing_event(
                    run,
                    kind="timing.triggers_enabled",
                    value=status.triggers_enabled,
                    status=status,
                )
            if previous.euv_transmitting() != status.euv_transmitting():
                self._emit_timing_event(
                    run,
                    kind="timing.euv_transmitting",
                    value=status.euv_transmitting(),
                    status=status,
                )
        run.last_timing_status = status

    def _open_timing_event_stream(self, run: _AcquisitionRun) -> None:
        if run.timing_stream is not None:
            return
        stream_name = "acquisition.timing"
        stream = RunEventStream(
            run.run_id,
            run_event_stream_id(run.run_id, uuids.UUID_EUV_ACQUISITION_CONTROLLER, stream_name),
            stream_name,
            uuids.UUID_EUV_ACQUISITION_CONTROLLER,
        )
        timestamp_ns = time.time_ns()
        self._run_event_emitter.emit(
            stream.event(
                STREAM_START_KIND,
                {"source": "EUV Acquisition", "session_id": str(run.session_id)},
                producer_unix_ns=timestamp_ns,
                producer_monotonic_ns=time.monotonic_ns(),
                capture_session_id=run.session_id,
                runtime_seconds=run.accumulator.transmitting_runtime_seconds,
            )
        )
        run.timing_stream = stream
        with self._timing_status_lock:
            status = self._timing_status
        if status is not None:
            self._record_timing_state(run, status)

    def _close_timing_event_stream(self, run: _AcquisitionRun, *, outcome: str) -> bool:
        if run.timing_stream is None or run.timing_stream_closed:
            return True
        timestamp_ns = time.time_ns()
        self._run_event_emitter.emit(
            run.timing_stream.event(
                STREAM_END_KIND,
                {"outcome": outcome},
                producer_unix_ns=timestamp_ns,
                producer_monotonic_ns=time.monotonic_ns(),
                capture_session_id=run.session_id,
                next_sequence=None if run.last_sequence is None else run.last_sequence + 1,
                runtime_seconds=run.accumulator.transmitting_runtime_seconds,
            )
        )
        run.timing_stream_closed = True
        if self._run_event_emitter.flush(5.0):
            return True
        self._log(
            "Timing event stream did not flush within five seconds; delivery will continue in the background.",
            level="ERROR",
            event="timing_event_flush_deferred",
            run_id=str(run.run_id),
        )
        return False

    def _ensure_persisted_exposure_graph(self, run_id: uuid.UUID, entry, *, context: str) -> None:
        try:
            result = ensure_exposure_graph(run_id, entry, self._data_path, allow_incomplete=True)
        except Exception as exc:
            self._log(
                f"Could not generate persisted exposure graph after {context}: {type(exc).__name__}: {exc}",
                level="ERROR",
                event="exposure_graph_generation_failed",
                run_id=str(run_id),
            )
            return
        self._log(
            f"Persisted exposure graph {result.status} after {context}.",
            event="exposure_graph_generation_completed",
            run_id=str(run_id),
            status=result.status,
        )

    def _can_start(self, settings: ExposureSettings, state: RunState) -> tuple[bool, bytes]:
        self._log("Checking whether EUV acquisition can start.", level="DEBUG", event="acquisition_start_check", run_id=str(state.get_uuid()))
        try:
            calibration = resolve_exposure_calibration(settings, self._data_path)
            frequency = settings.get_chopper_frequency_hz()
            if frequency is None or float(frequency) <= 0:
                raise ValueError("Exposure requires a positive configured chopper frequency.")
            frequency = float(frequency)
            target_dose = float(settings.get_target_dose()) if float(settings.get_target_dose()) > 0.1 else None
            target_time = float(settings.get_target_time()) if float(settings.get_target_time()) > 0.1 else None
            if target_dose is not None and target_time is not None:
                raise ValueError("Cannot set both target dose and target time.")
        except (TypeError, ValueError) as exc:
            self._log(f"Rejected acquisition start: {exc}", level="WARNING", event="acquisition_start_rejected", run_id=str(state.get_uuid()))
            return False, str(exc).encode("utf-8")
        with self._run_lock:
            if getattr(self, "_diagnostic", None) is not None or getattr(self, "_diagnostic_start_pending", False):
                self._log("Rejected acquisition start because diagnostics are active.", level="WARNING", event="acquisition_start_rejected", run_id=str(state.get_uuid()))
                return False, b"Acquisition diagnostics must be stopped before an exposure can start."
            if self._run is not None and self._run.run_id != state.get_uuid():
                self._log("Rejected acquisition start because a prior in-memory session remains.", level="WARNING", event="acquisition_start_rejected", run_id=str(state.get_uuid()), prior_run_id=str(self._run.run_id))
                return False, b"Previous acquisition session has not been released."
            if self._run is None:
                try:
                    self._check_capture_service_ready()
                except Exception as exc:
                    message = f"Digitizer capture is not ready: {exc}"
                    self._log(
                        message,
                        level="WARNING",
                        event="acquisition_start_rejected",
                        run_id=str(state.get_uuid()),
                    )
                    return False, message.encode("utf-8")
                self._run = _AcquisitionRun(
                    run_id=state.get_uuid(),
                    calibration=calibration,
                    target_dose=target_dose,
                    target_time=target_time,
                    chopper_frequency_hz=frequency,
                    accumulator=LiveDoseAccumulator(calibration),
                )
                self._log("Reserved acquisition state for exposure start.", event="acquisition_start_reserved", run_id=str(state.get_uuid()), calibration_profile_id=str(calibration.profile_id), calibration_revision=calibration.revision, chopper_frequency_hz=frequency)
        return super()._can_start(settings, state)

    def _check_capture_service_ready(self) -> None:
        capture_client = self._ensure_capture_client(force=True)
        status = capture_client.command("status")
        self._cache_board_status(status)
        raw_session = status.get("session")
        if raw_session is None:
            return
        session = CaptureSessionManifest.from_dict(raw_session)
        if session.purpose is CapturePurpose.DIAGNOSTIC:
            self._require_board_capabilities(status, "discard_diagnostic_session")
            self._discard_retained_diagnostic(session, capture_active=bool(status.get("capture_active")))
            return
        raise RuntimeError(
            f"The digitizer spool retains {session.state.value} session {session.session_id}; "
            "recover or release it before starting another exposure."
        )

    def _on_preinit(self, handle):
        with self._run_lock:
            run = self._require_run()
            try:
                self._log("Beginning digitizer capture preinitialization.", event="capture_preinit_started", run_id=str(run.run_id))
                self._ensure_capture_client(force=True)
                session_id = uuid.uuid4()
                started = self._capture_command("start_capture", {"session_id": str(session_id)})
                session_value = started.get("session")
                if isinstance(session_value, dict):
                    capture_session = CaptureSessionManifest.from_dict(session_value)
                    if capture_session.session_id != session_id:
                        raise ValueError("Digitizer start response returned the wrong capture session.")
                    run.source_kind = capture_session.source_kind
                    run.source_id = capture_session.source_id
                self._write_preinit_provenance(session_id, run)
                self._log("Persisted acquisition calibration and session provenance.", event="capture_provenance_written", run_id=str(run.run_id), session_id=str(session_id))
            except Exception as exc:
                self._log(f"Digitizer capture preinitialization failed: {type(exc).__name__}: {exc}", level="ERROR", event="capture_preinit_failed", run_id=str(run.run_id))
                if self._capture_client is not None:
                    try:
                        self._capture_command("stop_capture", {"reason": "Exposure preinit record persistence failed."})
                    except Exception as stop_exc:
                        self._log(f"Best-effort digitizer cleanup after preinit failure also failed: {type(stop_exc).__name__}: {stop_exc}", level="ERROR", event="capture_preinit_cleanup_failed", run_id=str(run.run_id))
                return False, f"Unable to start digitizer capture: {exc}".encode("utf-8")
            run.session_id = session_id
            self._open_timing_event_stream(run)
            self._log("Digitizer capture preinitialization completed.", event="capture_preinit_completed", run_id=str(run.run_id), session_id=str(session_id), source_kind=run.source_kind, source_id=run.source_id)
            self._preinit_pending = True
            self._stop_requested = False
        return super()._on_preinit(handle)

    def _on_start(self, handle):
        with self._run_lock:
            run = self._require_run()
            run.accumulator.set_running(True)
            run.running_started_monotonic = time.monotonic()
            self._start_pending = True
        return super()._on_start(handle)

    def _on_stop(self, handle):
        with self._run_lock:
            run = self._run
            if run is None:
                print("[EUV Acquisition] Stop received with no active acquisition state.", flush=True)
                return b"EUV acquisition had no active capture to stop."
            run.finalizing = True
            run.stop_requested_monotonic = time.monotonic()
            self._last_stop_feedback_monotonic = 0.0
            if run.session_id is None or self._capture_client is None:
                run.finalization_phase = "clearing rejected start"
                run.finalization_detail = "No capture session was opened."
                print(
                    f"[EUV Acquisition] Clearing rejected/preinit-only run {run.run_id}; no capture session exists.",
                    flush=True,
                )
            else:
                run.accumulator.set_running(False)
                run.stop_deadline_monotonic = time.monotonic() + self.STOP_DRAIN_CAP_SECONDS
                run.finalization_phase = "draining capture"
                run.finalization_detail = "Waiting for the final pulse and snapshot reports."
                print(
                    f"[EUV Acquisition] Finalizing run {run.run_id}; draining capture for up to "
                    f"{self.STOP_DRAIN_CAP_SECONDS:.1f}s.",
                    flush=True,
                )
        return b"EUV acquisition is stopping."

    def _on_continue_state(self):
        with self._run_lock:
            if self._run is not None:
                return True, self.EXP_IN_PROGRESS
        return False, b"EUV acquisition is idle."

    def _worker(self, stop_flag: StopFlag) -> None:
        self._reader = ExperimentReader(self._data_path, "exposure")
        try:
            while stop_flag.run():
                try:
                    self._maintain_capture_connection()
                    self._complete_pending_lifecycle_events()
                    self._process_recovery_requests()
                    self._process_control_requests()
                    self._consume_capture_events()
                    self._advance_diagnostic()
                    self._advance_finalization()
                    self._release_after_stopped()
                    self._publish_values()
                except Exception as exc:
                    self._handle_worker_failure(exc)
                time.sleep(0.01)
        finally:
            if self._reader is not None:
                self._reader.close()
                self._reader = None

    def _handle_worker_failure(self, exc: Exception) -> None:
        self._handle_exception(exc)
        with self._run_lock:
            run = self._run
            finalizing = run is not None and run.finalizing
            diagnostic = getattr(self, "_diagnostic", None)
        if finalizing:
            self._defer_finalization(run, f"{type(exc).__name__}: {exc}")
        elif run is not None:
            self._request_exposure_stop(f"Acquisition worker error: {type(exc).__name__}: {exc}")
        elif diagnostic is not None:
            self._fail_diagnostic(f"{type(exc).__name__}: {exc}")

    def _complete_pending_lifecycle_events(self) -> None:
        if self._preinit_pending:
            self._preinit_pending = False
            self._on_did_preinit(b"Digitizer capture is armed.")
        if self._start_pending:
            self._start_pending = False
            self._on_did_start(b"EUV acquisition is receiving pulse reports.")

    def _process_recovery_requests(self) -> None:
        try:
            handle = self._recovery_requests.get_nowait()
        except queue.Empty:
            return
        self._recovery_active = True
        try:
            self._log("Attempting orphaned capture recovery.", event="orphan_recovery_started")
            result = self._recover_orphaned_capture_session()
        except Exception as exc:
            self._log(f"Orphaned capture recovery failed: {exc}", level="ERROR")
            handle.fail(f"Orphaned capture recovery failed: {exc}".encode("utf-8"))
        else:
            self._log(result, level="INFO")
            handle.ret(result.encode("utf-8"))
        finally:
            self._recovery_active = False

    def _process_control_requests(self) -> None:
        try:
            request = self._control_requests.get_nowait()
        except queue.Empty:
            return
        deferred = False
        try:
            if request.action == "diagnostic_start":
                result = self._start_diagnostic(request.payload["mode"], request.handle)
                deferred = result is None
            elif request.action == "diagnostic_flush":
                result = self._flush_diagnostic()
            elif request.action == "diagnostic_stop":
                result = self._resume_diagnostic_stop("Operator stopped the acquisition diagnostic.")
                if result is None:
                    with self._run_lock:
                        self._require_diagnostic().completion_handles.append(request.handle)
                    deferred = True
            elif request.action == "simulator_set":
                result = self._set_simulator_control(request.payload)
            elif request.action == "simulator_restore":
                result = self._restore_simulator_controls()
            else:
                raise RuntimeError(f"Unknown acquisition control request {request.action!r}.")
        except Exception as exc:
            if request.action == "diagnostic_start":
                with self._run_lock:
                    self._diagnostic_start_pending = False
            self._log(
                f"Acquisition control request {request.action} failed: {type(exc).__name__}: {exc}",
                level="ERROR",
                event="acquisition_control_failed",
                action=request.action,
            )
            request.handle.fail(str(exc).encode("utf-8", errors="replace"))
        else:
            if not deferred:
                request.handle.ret(result)

    def _start_diagnostic(self, mode: str, handle) -> bytes | None:
        if mode not in {"continuous", "one_shot"}:
            raise ValueError(f"Unknown acquisition diagnostic mode {mode!r}.")
        with self._run_lock:
            if self._run is not None or self._diagnostic is not None:
                raise RuntimeError("Acquisition is no longer idle.")
            if not self._diagnostic_start_pending:
                raise RuntimeError("Acquisition diagnostic start was not reserved.")
        self._ensure_capture_client(force=True)
        status = self._capture_command("status", {})
        self._require_board_capabilities(
            status,
            "capture_purpose",
            "purge_snapshot",
            "discard_diagnostic_session",
        )
        raw_session = status.get("session")
        if raw_session is not None:
            retained = CaptureSessionManifest.from_dict(raw_session)
            if retained.purpose is not CapturePurpose.DIAGNOSTIC:
                raise RuntimeError(
                    f"The digitizer retains {retained.purpose.value} session {retained.session_id}; "
                    "diagnostics cannot clean or replace it."
                )
            self._discard_retained_diagnostic(retained, capture_active=bool(status.get("capture_active")))

        session_id = uuid.uuid4()
        started_monotonic = time.monotonic()
        try:
            result = self._capture_command(
                "start_capture",
                {"session_id": str(session_id), "purpose": CapturePurpose.DIAGNOSTIC.value},
            )
        except (ConnectionError, OSError, TimeoutError) as exc:
            self._retain_uncertain_diagnostic_start(
                session_id,
                mode,
                status,
                started_monotonic,
                f"Diagnostic start outcome is unknown after {type(exc).__name__}: {exc}",
            )
            raise
        try:
            session = self._diagnostic_session_from_status(result, expected_session_id=session_id)
        except Exception as exc:
            self._retain_uncertain_diagnostic_start(
                session_id,
                mode,
                status,
                started_monotonic,
                f"Diagnostic start response could not be verified: {type(exc).__name__}: {exc}",
            )
            raise
        diagnostic = _DiagnosticCapture(
            session_id=session_id,
            mode=mode,
            source_kind=session.source_kind,
            source_id=session.source_id,
            started_monotonic=started_monotonic,
        )
        if mode == "one_shot":
            diagnostic.completion_handles.append(handle)
        with self._run_lock:
            self._diagnostic = diagnostic
            self._diagnostic_start_pending = False
        self._log(
            f"Started {mode.replace('_', '-')} acquisition diagnostic {session_id}.",
            event="acquisition_diagnostic_started",
            session_id=str(session_id),
            mode=mode,
            source_kind=session.source_kind,
            source_id=session.source_id,
        )
        if mode == "one_shot":
            handle.feedback(b"Waiting for the first diagnostic pulse.")
            return None
        return f"Started continuous acquisition diagnostic {session_id}.".encode("utf-8")

    def _retain_uncertain_diagnostic_start(
        self,
        session_id: uuid.UUID,
        mode: str,
        status: dict,
        started_monotonic: float,
        detail: str,
    ) -> None:
        diagnostic = _DiagnosticCapture(
            session_id=session_id,
            mode=mode,
            source_kind=str(status.get("source_kind") or "unknown"),
            source_id=str(status.get("source_id") or "unknown"),
            started_monotonic=started_monotonic,
            state="error",
            terminal_error=detail,
        )
        with self._run_lock:
            self._diagnostic = diagnostic
            self._diagnostic_start_pending = False
            self._diagnostic_error = detail
        self._log(
            detail,
            level="ERROR",
            event="acquisition_diagnostic_start_uncertain",
            session_id=str(session_id),
            mode=mode,
        )

    def _flush_diagnostic(self) -> bytes:
        with self._run_lock:
            diagnostic = self._require_diagnostic()
            if diagnostic.mode != "continuous" or diagnostic.state != "running":
                raise RuntimeError("A continuous acquisition diagnostic is not running.")
        result = self._capture_command("flush_snapshot", {})
        self._queue_diagnostic_snapshots_from_status(result)
        self._import_pending_diagnostic_snapshots()
        with self._run_lock:
            snapshot_count = len(self._require_diagnostic().processed_snapshot_ids)
        return f"Flushed diagnostic; {snapshot_count} snapshot(s) transferred.".encode("utf-8")

    def _stop_diagnostic(self, reason: str) -> None:
        with self._run_lock:
            diagnostic = self._require_diagnostic()
            if diagnostic.stop_sent:
                return
        try:
            result = self._capture_command("stop_capture", {"reason": reason})
        except Exception as exc:
            self._fail_diagnostic(
                f"Diagnostic stop outcome is unknown after {type(exc).__name__}: {exc}"
            )
            raise
        with self._run_lock:
            diagnostic = self._require_diagnostic()
            diagnostic.stop_sent = True
            diagnostic.stop_reason = reason
            diagnostic.state = "finalizing"
        self._queue_diagnostic_snapshots_from_status(result)

    def _resume_diagnostic_stop(self, reason: str) -> bytes | None:
        with self._run_lock:
            diagnostic = self._require_diagnostic()
            failed = diagnostic.state == "error"
            session_id = diagnostic.session_id
        if not failed:
            self._stop_diagnostic(reason)
            return None
        self._ensure_capture_client(force=True)
        status = self._capture_command("status", {})
        if status.get("session") is None:
            with self._run_lock:
                diagnostic = self._require_diagnostic()
                if diagnostic.session_id != session_id:
                    raise RuntimeError("Active diagnostic changed while its stop was being reconciled.")
                self._diagnostic = None
            return b"Cleared failed diagnostic state; the digitizer has no retained session."
        session = self._diagnostic_session_from_status(status, expected_session_id=session_id)
        if status.get("capture_active") or session.state is CaptureSessionState.ACTIVE:
            status = self._capture_command("stop_capture", {"reason": reason})
            session = self._diagnostic_session_from_status(status, expected_session_id=session_id)
        with self._run_lock:
            diagnostic = self._require_diagnostic()
            diagnostic.stop_sent = True
            diagnostic.stop_reason = reason
            diagnostic.state = "finalizing"
            self._merge_diagnostic_snapshots(diagnostic, session.snapshots)
            return None

    def _advance_diagnostic(self) -> None:
        with self._run_lock:
            diagnostic = self._diagnostic
            if diagnostic is None:
                return
            if diagnostic.state == "error":
                should_reconcile_stop = (
                    not diagnostic.stop_sent
                    and self._capture_client is not None
                    and time.monotonic() >= diagnostic.next_cleanup_attempt_monotonic
                )
                if should_reconcile_stop:
                    diagnostic.next_cleanup_attempt_monotonic = (
                        time.monotonic() + self.CAPTURE_RECONNECT_DELAY_SECONDS
                    )
            else:
                should_reconcile_stop = False
            should_stop = diagnostic.mode == "one_shot" and diagnostic.report_count >= 1 and not diagnostic.stop_sent
            timed_out = (
                diagnostic.mode == "one_shot"
                and diagnostic.report_count == 0
                and time.monotonic() - diagnostic.started_monotonic >= self.ONE_SHOT_TIMEOUT_SECONDS
                and not diagnostic.stop_sent
            )
            if timed_out:
                diagnostic.terminal_error = "Timed out waiting for the first diagnostic pulse."
        if diagnostic.state == "error":
            if should_reconcile_stop:
                self._reconcile_failed_diagnostic_stop()
            return
        if should_stop:
            self._stop_diagnostic("One-shot diagnostic captured its first pulse.")
        elif timed_out:
            self._stop_diagnostic("One-shot diagnostic timed out before receiving a pulse.")

        with self._run_lock:
            diagnostic = self._diagnostic
            if diagnostic is None or diagnostic.state != "finalizing":
                return
        listing = self._capture_command("list_snapshots", {})
        snapshots = tuple(StoredSnapshot.from_dict(value) for value in listing.get("snapshots", []))
        with self._run_lock:
            diagnostic = self._require_diagnostic()
            self._merge_diagnostic_snapshots(diagnostic, snapshots)
        self._import_pending_diagnostic_snapshots()
        with self._run_lock:
            diagnostic = self._require_diagnostic()
            if diagnostic.pending_snapshots:
                return
            if any(snapshot.manifest.snapshot_id not in diagnostic.processed_snapshot_ids for snapshot in snapshots):
                return
            session_id = diagnostic.session_id
        self._capture_command("discard_diagnostic_session", {"session_id": str(session_id)})
        self._complete_diagnostic()

    def _reconcile_failed_diagnostic_stop(self) -> None:
        with self._run_lock:
            diagnostic = self._require_diagnostic()
            session_id = diagnostic.session_id
        try:
            status = self._capture_command("status", {})
            if status.get("session") is None:
                with self._run_lock:
                    diagnostic = self._require_diagnostic()
                    if diagnostic.session_id == session_id and diagnostic.state == "error":
                        self._diagnostic = None
                self._log(
                    "Cleared failed diagnostic ownership because the digitizer has no retained session.",
                    level="WARNING",
                    event="acquisition_failed_diagnostic_absent",
                    session_id=str(session_id),
                )
                return
            session = self._diagnostic_session_from_status(status, expected_session_id=session_id)
            if status.get("capture_active") or session.state is CaptureSessionState.ACTIVE:
                stopped = self._capture_command(
                    "stop_capture",
                    {"reason": "Stopping capture after an acquisition diagnostic failure."},
                )
                session = self._diagnostic_session_from_status(stopped, expected_session_id=session_id)
        except Exception as exc:
            self._log(
                f"Could not stop failed diagnostic capture {session_id}: {type(exc).__name__}: {exc}",
                level="ERROR",
                event="acquisition_failed_diagnostic_stop_deferred",
                session_id=str(session_id),
            )
            return
        with self._run_lock:
            diagnostic = self._require_diagnostic()
            if diagnostic.session_id != session_id or diagnostic.state != "error":
                return
            diagnostic.stop_sent = True
            diagnostic.stop_reason = "Digitizer capture stopped after diagnostic failure."
            self._merge_diagnostic_snapshots(diagnostic, session.snapshots)
        self._log(
            "Stopped the digitizer capture after a diagnostic failure; artifacts await explicit cleanup.",
            level="WARNING",
            event="acquisition_failed_diagnostic_stopped",
            session_id=str(session_id),
        )

    def _queue_diagnostic_snapshots_from_status(self, status: dict) -> None:
        session = self._diagnostic_session_from_status(status)
        with self._run_lock:
            diagnostic = self._require_diagnostic()
            if session.session_id != diagnostic.session_id:
                raise RuntimeError("Digitizer status returned another diagnostic session.")
            self._merge_diagnostic_snapshots(diagnostic, session.snapshots)

    @staticmethod
    def _merge_diagnostic_snapshots(
        diagnostic: _DiagnosticCapture,
        snapshots: tuple[StoredSnapshot, ...],
    ) -> None:
        for stored_snapshot in snapshots:
            manifest = stored_snapshot.manifest
            if manifest.session_id != diagnostic.session_id:
                raise RuntimeError("Digitizer listed a snapshot from another diagnostic session.")
            if stored_snapshot.acknowledged:
                diagnostic.processed_snapshot_ids.add(manifest.snapshot_id)
                diagnostic.pending_snapshots.pop(manifest.snapshot_id, None)
            elif manifest.snapshot_id not in diagnostic.processed_snapshot_ids:
                diagnostic.pending_snapshots[manifest.snapshot_id] = manifest

    def _import_pending_diagnostic_snapshots(self) -> None:
        with self._run_lock:
            diagnostic = self._require_diagnostic()
            manifests = tuple(diagnostic.pending_snapshots.values())
        for manifest in manifests:
            self._import_diagnostic_snapshot(manifest)

    def _import_diagnostic_snapshot(self, manifest) -> None:
        with self._run_lock:
            diagnostic = self._require_diagnostic()
            if manifest.session_id != diagnostic.session_id:
                raise RuntimeError("Diagnostic snapshot belongs to another session.")
            if manifest.snapshot_id in diagnostic.processed_snapshot_ids:
                diagnostic.pending_snapshots.pop(manifest.snapshot_id, None)
                return
        importer = self._artifact_importer
        publisher = self._preview_publisher
        if importer is None or publisher is None:
            raise RuntimeError("Diagnostic artifact transfer or preview publication is unavailable.")
        local_path = None
        expected_path = None if self._temporary_directory is None else self._temporary_directory / manifest.filename
        try:
            local_path = importer.fetch_verified_snapshot(manifest)
            preview = build_acquisition_preview(local_path, manifest, context="diagnostic")
            publisher.value = preview.encode()
            importer.acknowledge_snapshot(manifest)
            self._capture_command("purge_snapshot", {"snapshot_id": str(manifest.snapshot_id)})
        finally:
            if local_path is not None:
                local_path.unlink(missing_ok=True)
            if expected_path is not None:
                expected_path.unlink(missing_ok=True)
        with self._run_lock:
            diagnostic = self._require_diagnostic()
            diagnostic.processed_snapshot_ids.add(manifest.snapshot_id)
            diagnostic.pending_snapshots.pop(manifest.snapshot_id, None)
        self._log(
            "Published and purged diagnostic acquisition snapshot.",
            event="acquisition_diagnostic_snapshot_completed",
            session_id=str(manifest.session_id),
            snapshot_id=str(manifest.snapshot_id),
        )

    def _discard_retained_diagnostic(
        self,
        session: CaptureSessionManifest,
        *,
        capture_active: bool,
    ) -> None:
        if session.purpose is not CapturePurpose.DIAGNOSTIC:
            raise RuntimeError("Refusing to automatically discard a non-diagnostic capture session.")
        if capture_active or session.state is CaptureSessionState.ACTIVE:
            stopped = self._capture_command(
                "stop_capture",
                {"reason": "Cleaning a retained diagnostic session before a new operation."},
            )
            session = self._diagnostic_session_from_status(stopped, expected_session_id=session.session_id)
        self._capture_command("discard_diagnostic_session", {"session_id": str(session.session_id)})
        capture_client = self._capture_client
        if capture_client is not None:
            self._drain_capture_events(capture_client)
        self._log(
            "Discarded a retained diagnostic capture session.",
            event="acquisition_stale_diagnostic_discarded",
            session_id=str(session.session_id),
        )

    @staticmethod
    def _require_board_capabilities(status: dict, *required: str) -> None:
        capabilities = status.get("capabilities") if isinstance(status, dict) else None
        missing = [name for name in required if not isinstance(capabilities, dict) or capabilities.get(name) is not True]
        if missing:
            raise RuntimeError(
                "Digitizer service does not advertise required capabilities: " + ", ".join(missing) + "."
            )

    def _diagnostic_session_from_status(
        self,
        status: dict,
        *,
        expected_session_id: uuid.UUID | None = None,
    ) -> CaptureSessionManifest:
        raw_session = status.get("session") if isinstance(status, dict) else None
        if raw_session is None:
            raise RuntimeError("Digitizer status does not contain the diagnostic capture session.")
        session = CaptureSessionManifest.from_dict(raw_session)
        if session.purpose is not CapturePurpose.DIAGNOSTIC:
            raise RuntimeError("Digitizer status contains a non-diagnostic capture session.")
        if expected_session_id is not None and session.session_id != expected_session_id:
            raise RuntimeError("Digitizer status returned the wrong diagnostic session.")
        return session

    def _complete_diagnostic(self) -> None:
        with self._run_lock:
            diagnostic = self._require_diagnostic()
            snapshot_count = len(diagnostic.processed_snapshot_ids)
            self._last_diagnostic_summary = {
                "mode": diagnostic.mode,
                "report_count": diagnostic.report_count,
                "snapshot_count": snapshot_count,
            }
            self._diagnostic = None
            self._diagnostic_error = diagnostic.terminal_error
        if diagnostic.terminal_error is None:
            message = (
                f"Completed {diagnostic.mode.replace('_', '-')} acquisition diagnostic "
                f"{diagnostic.session_id} with {diagnostic.report_count} pulse report(s) and "
                f"{snapshot_count} transferred snapshot(s)."
            )
            for handle in diagnostic.completion_handles:
                handle.ret(message.encode("utf-8"))
            level = "INFO"
        else:
            message = diagnostic.terminal_error
            for handle in diagnostic.completion_handles:
                handle.fail(message.encode("utf-8", errors="replace"))
            level = "ERROR"
        self._log(
            message,
            level=level,
            event="acquisition_diagnostic_completed",
            session_id=str(diagnostic.session_id),
            mode=diagnostic.mode,
            report_count=diagnostic.report_count,
        )

    def _fail_diagnostic(self, detail: str) -> None:
        with self._run_lock:
            diagnostic = self._diagnostic
            if diagnostic is None:
                return
            diagnostic.state = "error"
            diagnostic.terminal_error = detail
            diagnostic.next_cleanup_attempt_monotonic = 0.0
            handles = tuple(diagnostic.completion_handles)
            diagnostic.completion_handles.clear()
            self._diagnostic_error = detail
        for handle in handles:
            handle.fail(detail.encode("utf-8", errors="replace"))
        self._log(
            f"Acquisition diagnostic failed: {detail}",
            level="ERROR",
            event="acquisition_diagnostic_failed",
            session_id=str(diagnostic.session_id),
        )

    def _set_simulator_control(self, payload: dict) -> bytes:
        status = self._capture_command("status", {})
        self._require_board_capabilities(status, "simulator_controls")
        result = self._capture_command("set_simulator_control", payload)
        return json.dumps(result["simulator"], allow_nan=False, separators=(",", ":")).encode("utf-8")

    def _restore_simulator_controls(self) -> bytes:
        status = self._capture_command("status", {})
        self._require_board_capabilities(status, "simulator_controls")
        result = self._capture_command("restore_simulator_controls", {})
        return json.dumps(result["simulator"], allow_nan=False, separators=(",", ":")).encode("utf-8")

    def _require_diagnostic(self) -> _DiagnosticCapture:
        if self._diagnostic is None:
            raise RuntimeError("No acquisition diagnostic is active.")
        return self._diagnostic

    def _consume_capture_events(self) -> None:
        capture_client = self._capture_client
        if capture_client is None:
            return
        with self._run_lock:
            run = self._run
            diagnostic = self._diagnostic
            if run is None and diagnostic is None:
                self._drain_capture_events(capture_client)
                return
        if run is None:
            if diagnostic.state == "error":
                return
            self._consume_diagnostic_events(capture_client)
            return
        while True:
            try:
                report_data = capture_client.get_report(timeout=0)
            except queue.Empty:
                break
            try:
                self._ingest_report(PulseReport.from_dict(report_data))
            except (ValueError, PulseSequenceGap) as exc:
                self._request_exposure_stop(f"Digitizer pulse-report error: {exc}")
        while True:
            try:
                manifest = capture_client.get_snapshot(timeout=0)
            except queue.Empty:
                break
            try:
                with self._run_lock:
                    run = self._require_run()
                    if manifest.snapshot_id not in run.imported_snapshot_ids:
                        run.pending_snapshots[manifest.snapshot_id] = manifest
            except Exception as exc:
                self._request_exposure_stop(str(exc))
        self._import_pending_snapshots()

    def _consume_diagnostic_events(self, capture_client: AcquisitionClient) -> None:
        while True:
            try:
                report = PulseReport.from_dict(capture_client.get_report(timeout=0))
            except queue.Empty:
                break
            with self._run_lock:
                diagnostic = self._require_diagnostic()
                if report.session_id != diagnostic.session_id:
                    raise RuntimeError("Diagnostic pulse report belongs to another capture session.")
                if diagnostic.last_sequence is not None and report.sequence <= diagnostic.last_sequence:
                    raise RuntimeError("Diagnostic pulse reports are out of order.")
                diagnostic.last_sequence = report.sequence
                diagnostic.report_count += 1
        while True:
            try:
                manifest = capture_client.get_snapshot(timeout=0)
            except queue.Empty:
                break
            with self._run_lock:
                diagnostic = self._require_diagnostic()
                if manifest.session_id != diagnostic.session_id:
                    raise RuntimeError("Diagnostic snapshot belongs to another capture session.")
                if manifest.snapshot_id not in diagnostic.processed_snapshot_ids:
                    diagnostic.pending_snapshots[manifest.snapshot_id] = manifest
        while True:
            try:
                reason = capture_client.get_stop_reason(timeout=0)
            except queue.Empty:
                break
            with self._run_lock:
                diagnostic = self._require_diagnostic()
                if not diagnostic.stop_sent:
                    diagnostic.stop_sent = True
                    diagnostic.state = "finalizing"
                    diagnostic.stop_reason = reason
                    diagnostic.terminal_error = f"Digitizer stopped the diagnostic capture: {reason}"
        self._import_pending_diagnostic_snapshots()

    @staticmethod
    def _drain_capture_events(capture_client: AcquisitionClient) -> None:
        for getter in (capture_client.get_report, capture_client.get_snapshot, capture_client.get_stop_reason):
            while True:
                try:
                    getter(timeout=0)
                except queue.Empty:
                    break

    def _ingest_report(self, report: PulseReport) -> None:
        with self._run_lock:
            run = self._require_run()
            if report.session_id != run.session_id:
                raise PulseSequenceGap("Pulse report session does not match the active exposure.")
            run.accumulator.set_transmitting(self._is_laser_transmitting())
            update = run.accumulator.ingest(report)
            if not update.accepted:
                return
            if run.last_report_monotonic_ns is None or report.captured_at_monotonic_ns - run.last_report_monotonic_ns > 250_000_000:
                run.consecutive_timed_pulses = 1
            else:
                run.consecutive_timed_pulses += 1
            run.last_report_monotonic_ns = report.captured_at_monotonic_ns
            run.last_sequence = report.sequence
            run.report_totals[report.sequence] = (
                update.accumulated_dose_mj_cm2,
                update.transmitting_runtime_seconds,
            )
            run.last_pulse_clipped = bool(report.analysis.quality & PulseQuality.CLIPPED)
            if run.last_pulse_clipped:
                run.clipped_pulse_count += 1
            if run.pulse_loss_active and run.consecutive_timed_pulses >= 10:
                run.recovery_ready = True
            run.last_pulse_monotonic = time.monotonic()
            if run.accumulator.running and run.target_dose is not None and update.accumulated_dose_mj_cm2 >= run.target_dose:
                self._request_exposure_stop(f"Target dose of {run.target_dose} mJ/cm2 reached.")
            if run.accumulator.running and run.target_time is not None and update.transmitting_runtime_seconds >= run.target_time:
                self._request_exposure_stop(f"Target time of {run.target_time} s reached.")

    def _import_pending_snapshots(self) -> None:
        with self._run_lock:
            run = self._run
            if run is None:
                return
            manifests = tuple(run.pending_snapshots.values())
        for manifest in manifests:
            with self._run_lock:
                run = self._require_run()
                if manifest.snapshot_id not in run.pending_snapshots:
                    continue
                if manifest.final_sequence not in run.report_totals:
                    continue
            try:
                self._import_snapshot(manifest)
            except Exception as exc:
                with self._run_lock:
                    finalizing = self._run is not None and self._run.finalizing
                if finalizing:
                    raise RuntimeError(
                        f"Could not reconcile snapshot {manifest.snapshot_id} during exposure finalization: {exc}"
                    ) from exc
                self._request_exposure_stop(str(exc))

    def _import_snapshot(self, manifest) -> None:
        with self._run_lock:
            run = self._require_run()
            if manifest.snapshot_id in run.imported_snapshot_ids:
                run.pending_snapshots.pop(manifest.snapshot_id, None)
                return
            totals = run.report_totals.get(manifest.final_sequence)
            if totals is None:
                raise RuntimeError(f"Snapshot {manifest.snapshot_id} arrived before its final pulse report.")
            record = self._get_record(run.run_id)
            assert self._artifact_importer is not None
            timeline_point = CaptureTimelinePoint(
                snapshot_id=manifest.snapshot_id,
                final_sequence=manifest.final_sequence,
                cumulative_dose_mj_cm2=totals[0],
                cumulative_runtime_seconds=totals[1],
            )
            self._log("Importing verified digitizer snapshot.", event="snapshot_import_started", run_id=str(run.run_id), session_id=str(run.session_id), snapshot_id=str(manifest.snapshot_id), final_sequence=manifest.final_sequence)
            temporary_paths: set[Path] = set()
            temporary_directory = getattr(self, "_temporary_directory", None)
            if temporary_directory is not None:
                temporary_paths.add(Path(temporary_directory) / manifest.filename)
            try:
                path = self._artifact_importer.import_snapshot(
                    record,
                    manifest,
                    before_ack=lambda entry: append_capture_timeline_point(entry, timeline_point),
                    after_persist=lambda _entry, local_path: self._publish_experiment_preview(
                        temporary_paths,
                        local_path,
                        manifest,
                        run.run_id,
                    ),
                )
                temporary_paths.add(path)
            finally:
                for temporary_path in temporary_paths:
                    temporary_path.unlink(missing_ok=True)
            run.imported_snapshot_ids.add(manifest.snapshot_id)
            self._log("Imported and acknowledged digitizer snapshot.", event="snapshot_import_completed", run_id=str(run.run_id), snapshot_id=str(manifest.snapshot_id))
            run.pending_snapshots.pop(manifest.snapshot_id, None)
            run.report_totals.pop(manifest.final_sequence, None)
            if self._segment_publisher is not None:
                self._segment_publisher.value = segment_bytes.encode([run.run_id.bytes, manifest.snapshot_id.bytes])

    def _publish_experiment_preview(
        self,
        temporary_paths: set[Path],
        local_path: Path,
        manifest,
        run_id: uuid.UUID,
    ) -> None:
        temporary_paths.add(local_path)
        publisher = getattr(self, "_preview_publisher", None)
        if publisher is None:
            return
        try:
            preview = build_acquisition_preview(local_path, manifest, context="experiment", run_id=run_id)
            publisher.value = preview.encode()
        except Exception as exc:
            self._log(
                f"Could not publish acquisition preview for snapshot {manifest.snapshot_id}: "
                f"{type(exc).__name__}: {exc}",
                level="WARNING",
                event="acquisition_preview_failed",
                run_id=str(run_id),
                snapshot_id=str(manifest.snapshot_id),
            )

    def _advance_finalization(self) -> None:
        with self._run_lock:
            run = self._run
            capture_client = self._capture_client
            if run is None or not run.finalizing:
                return
            self._keep_stop_event_alive(run)
            if time.monotonic() - run.stop_requested_monotonic < self.STOP_ACK_DELAY_SECONDS:
                return
            if run.session_id is None:
                self._complete_stop_without_capture(run)
                return
            if capture_client is None:
                self._defer_finalization(run, "The digitizer connection is unavailable; its spool was retained for recovery.")
                return
            quiet = run.last_pulse_monotonic is None or time.monotonic() - run.last_pulse_monotonic >= self.STOP_QUIET_SECONDS
            should_stop_capture = (not self._is_laser_transmitting() and quiet) or time.monotonic() >= run.stop_deadline_monotonic
            try:
                if not run.stop_capture_sent and should_stop_capture:
                    self._set_finalization_status(run, "stopping capture", "Requesting the digitizer stop and final snapshot flush.")
                    self._capture_command("stop_capture", {"reason": "Exposure stop drain complete."})
                    run.stop_capture_sent = True
                if not run.stop_capture_sent:
                    return
                listing = self._capture_command("list_snapshots", {})
                stored = tuple(StoredSnapshot.from_dict(value) for value in listing["snapshots"])
                for snapshot in stored:
                    if snapshot.manifest.snapshot_id not in run.imported_snapshot_ids:
                        run.pending_snapshots[snapshot.manifest.snapshot_id] = snapshot.manifest
                if run.pending_snapshots:
                    self._set_finalization_status(
                        run,
                        "waiting for pulse reports",
                        f"Waiting to reconcile {len(run.pending_snapshots)} snapshot(s).",
                    )
                    self._import_pending_snapshots()
                    return
                if len(run.imported_snapshot_ids) != len(stored):
                    self._set_finalization_status(run, "reconciling snapshots", "Waiting for transferred artifacts to become available.")
                    return
                self._set_finalization_status(run, "preparing dose analysis", "Recalculating the final dose analysis before exposure finalization.")
                record = self._get_record(run.run_id)
                result = analyze_experiment_entry(
                    run.run_id,
                    record.get_record(),
                    runtime_seconds=run.accumulator.transmitting_runtime_seconds,
                )
                self._set_finalization_status(run, "promoting dose analysis", "Writing final dose and runtime tags.")
                self._promote_final_analysis(run, result)
            except Exception as exc:
                self._defer_finalization(run, f"{type(exc).__name__}: {exc}")
                return
            run.finalizing = False
            run.release_pending = True
            if self._close_timing_event_stream(run, outcome="STOPPED"):
                self._ensure_persisted_exposure_graph(run.run_id, record.get_record(), context="final dose promotion")
            self._on_did_stop(b"EUV pulse reports and snapshots reconciled; final dose tags were promoted.")

    def _complete_stop_without_capture(self, run: _AcquisitionRun) -> None:
        message = f"Discarding preinit-only acquisition state for rejected run {run.run_id}."
        print(f"[EUV Acquisition] {message}", flush=True)
        self._log(
            message,
            level="INFO",
            event="acquisition_rejected_before_capture",
        )
        self._run = None
        self._on_did_stop(b"EUV acquisition cleared a rejected start before capture opened.")

    def _defer_finalization(self, run: _AcquisitionRun, detail: str) -> None:
        self._deferred_finalization_detail = detail
        print(
            f"[EUV Acquisition] Finalization deferred for run {run.run_id}: {detail}. "
            "Digitizer artifacts were retained for explicit recovery.",
            flush=True,
        )
        self._log(
            f"Deferring acquisition finalization for run {run.run_id}: {detail}",
            level="ERROR",
            event="acquisition_finalization_deferred",
        )
        if run.session_id is not None and self._capture_client is not None:
            try:
                status = self._capture_command("status", {})
                if status.get("capture_active"):
                    self._capture_command(
                        "stop_capture",
                        {"reason": "Exposure finalization deferred; retaining artifacts for recovery."},
                    )
            except Exception as stop_exc:
                self._log(
                    f"Could not stop digitizer capture before deferred recovery: {type(stop_exc).__name__}: {stop_exc}",
                    level="ERROR",
                    event="acquisition_deferred_stop_failed",
                    run_id=str(run.run_id),
                )
        self._run = None
        self._close_timing_event_stream(run, outcome="ABORTED")
        self._on_did_stop(
            b"Acquisition finalization deferred; digitizer artifacts remain retained for explicit recovery."
        )

    def _set_finalization_status(self, run: _AcquisitionRun, phase: str, detail: str) -> None:
        if run.finalization_phase == phase and run.finalization_detail == detail:
            return
        run.finalization_phase = phase
        run.finalization_detail = detail
        print(f"[EUV Acquisition] Finalizing run {run.run_id}: {phase}: {detail}", flush=True)
        self._log(
            f"Finalizing acquisition run {run.run_id}: {phase}: {detail}",
            level="INFO",
            event="acquisition_finalization_progress",
        )

    def _keep_stop_event_alive(self, run: _AcquisitionRun) -> None:
        now = time.monotonic()
        if now - self._last_stop_feedback_monotonic < self.STOP_FEEDBACK_INTERVAL_SECONDS:
            return
        phase = run.finalization_phase or "finalizing"
        detail = run.finalization_detail or "Reconciling digitizer capture artifacts."
        message = f"EUV acquisition {phase}: {detail}".encode("utf-8", errors="replace")
        self._on_stop_feedback(message)
        self._last_stop_feedback_monotonic = now
        self._log(
            "Renewed exposure stop event while acquisition finalization is active.",
            level="DEBUG",
            event="acquisition_stop_feedback",
            run_id=str(run.run_id),
            phase=phase,
        )

    def _release_after_stopped(self) -> None:
        with self._run_lock:
            run = self._run
            if run is None or not run.release_pending:
                return
            try:
                self._set_finalization_status(run, "releasing spool", "Releasing acknowledged digitizer artifacts after exposure stop.")
                self._capture_command("release_snapshots", {})
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc}"
                self._deferred_finalization_detail = detail
                self._log(
                    f"Final dose promotion or spool release failed for run {run.run_id}: {detail}; explicit recovery is required.",
                    level="ERROR",
                    event="capture_release_failed",
                    run_id=str(run.run_id),
                )
                self._run = None
                return
            self._log("Released digitizer spool after stopped exposure.", event="capture_release_completed", run_id=str(run.run_id))
            self._run = None

    def _promote_final_analysis(self, run: _AcquisitionRun, result: DoseAnalysisResult) -> None:
        record = self._get_record(run.run_id)
        revision = DoseAnalysisRevision(uuid.uuid4(), time.time(), result)
        write_analysis_revision(record.get_record(), revision, promote=True)
        self._log(
            "Promoted final acquisition dose analysis to exposure tags.",
            event="dose_analysis_promoted",
            run_id=str(run.run_id),
            dose_mj_cm2=result.total_dose_mj_cm2,
            runtime_seconds=result.runtime_seconds,
        )

    def _publish_values(self) -> None:
        if time.monotonic() - self._last_publish < self.STATUS_UPDATE_SECONDS:
            return
        self._last_publish = time.monotonic()
        with self._run_lock:
            run = self._run
            diagnostic = self._diagnostic
            board_status = dict(self._board_status)
            capabilities = board_status.get("capabilities")
            simulator = board_status.get("simulator")
            if run is None and diagnostic is None:
                dose = 0.0
                runtime = 0.0
                status = {
                    "state": "recovery_required" if self._deferred_finalization_detail is not None else "idle",
                    "capture_connected": self._capture_client is not None,
                    "source_kind": board_status.get("source_kind"),
                    "source_id": board_status.get("source_id"),
                    "capabilities": capabilities,
                    "simulator": simulator,
                    "last_sequence": None,
                    "clipped_pulse_count": 0,
                    "last_pulse_clipped": False,
                    "last_pulse_age_seconds": None,
                    "pulse_loss": False,
                    "recovery_ready": False,
                    "resume_authorized": False,
                    "release_pending": False,
                    "finalization_phase": None,
                    "finalization_detail": self._deferred_finalization_detail,
                    "diagnostic_error": self._diagnostic_error,
                    "last_diagnostic": self._last_diagnostic_summary,
                }
            elif run is not None:
                dose = run.accumulator.accumulated_dose_mj_cm2
                runtime = run.accumulator.transmitting_runtime_seconds
                pulse_age = None if run.last_pulse_monotonic is None else time.monotonic() - run.last_pulse_monotonic
                with self._timing_status_lock:
                    timing = self._timing_status
                waiting_for_initial_pulse = run.last_pulse_monotonic is None and run.running_started_monotonic is not None and time.monotonic() - run.running_started_monotonic <= 5.0
                pulse_loss = (
                    run.accumulator.running
                    and timing is not None
                    and timing.triggers_enabled
                    and not waiting_for_initial_pulse
                    and (pulse_age is None or pulse_age > 0.25)
                )
                if pulse_loss:
                    run.pulse_loss_active = True
                    run.recovery_ready = False
                    run.resume_authorized = False
                status = {
                    "state": "finalizing" if run.finalizing or run.release_pending else "running",
                    "session_id": str(run.session_id) if run.session_id else None,
                    "capture_connected": self._capture_client is not None,
                    "source_kind": run.source_kind,
                    "source_id": run.source_id,
                    "capabilities": capabilities,
                    "simulator": simulator,
                    "last_sequence": run.last_sequence,
                    "clipped_pulse_count": run.clipped_pulse_count,
                    "last_pulse_clipped": run.last_pulse_clipped,
                    "last_pulse_age_seconds": pulse_age,
                    "pulse_loss": pulse_loss,
                    "recovery_ready": run.recovery_ready,
                    "resume_authorized": run.resume_authorized,
                    "release_pending": run.release_pending,
                    "finalization_phase": run.finalization_phase,
                    "finalization_detail": run.finalization_detail,
                    "pending_snapshot_count": len(run.pending_snapshots),
                    "imported_snapshot_count": len(run.imported_snapshot_ids),
                }
                health = AcquisitionHealth(
                    capture_active=self._capture_client is not None,
                    session_id=run.session_id,
                    last_sequence=run.last_sequence,
                    last_pulse_age_seconds=pulse_age,
                    pulse_loss=pulse_loss,
                    recovery_ready=run.recovery_ready,
                    resume_authorized=run.resume_authorized,
                    reason="Pulse reports stopped while laser timing expected triggers." if pulse_loss else None,
                )
            else:
                dose = 0.0
                runtime = 0.0
                status = {
                    "state": f"diagnostic_{diagnostic.state}",
                    "session_id": str(diagnostic.session_id),
                    "capture_connected": self._capture_client is not None,
                    "source_kind": diagnostic.source_kind,
                    "source_id": diagnostic.source_id,
                    "capabilities": capabilities,
                    "simulator": simulator,
                    "last_sequence": diagnostic.last_sequence,
                    "clipped_pulse_count": 0,
                    "last_pulse_clipped": False,
                    "last_pulse_age_seconds": None,
                    "pulse_loss": False,
                    "recovery_ready": False,
                    "resume_authorized": False,
                    "release_pending": False,
                    "finalization_phase": diagnostic.state if diagnostic.state != "running" else None,
                    "finalization_detail": diagnostic.terminal_error or diagnostic.stop_reason,
                    "diagnostic_mode": diagnostic.mode,
                    "diagnostic_report_count": diagnostic.report_count,
                    "pending_snapshot_count": len(diagnostic.pending_snapshots),
                    "processed_snapshot_count": len(diagnostic.processed_snapshot_ids),
                    "diagnostic_elapsed_seconds": time.monotonic() - diagnostic.started_monotonic,
                    "diagnostic_error": diagnostic.terminal_error,
                }
                health = AcquisitionHealth(
                    diagnostic.state == "running",
                    diagnostic.session_id,
                    diagnostic.last_sequence,
                    None,
                    False,
                    False,
                    False,
                )
            if run is None and diagnostic is None:
                health = AcquisitionHealth(False, None, None, None, False, False, False)
            status["accumulated_dose_mj_cm2"] = dose
            status["transmitting_runtime_seconds"] = runtime
        if self._dose_publisher is not None:
            self._dose_publisher.value = dose
            self._time_publisher.value = runtime
            self._status_publisher.value = __import__("json").dumps(status, allow_nan=False, separators=(",", ":")).encode("utf-8")
            self._health_publisher.value = health.encode()

    def _request_exposure_stop(self, reason: str) -> None:
        if self._stop_requested or self._stop_exposure_event is None:
            return
        self._stop_requested = True
        self._log(reason, level="WARNING", event="exposure_stop_requested")
        self._stop_exposure_event.call(reason.encode("utf-8"), [])

    def _is_laser_transmitting(self) -> bool:
        with self._timing_status_lock:
            status = self._timing_status
            received_at = self._timing_status_received_at
        if status is None or time.monotonic() - received_at > 0.5:
            return False
        return status.euv_transmitting()

    def _ensure_capture_client(self, *, force: bool = False) -> AcquisitionClient:
        capture_client = getattr(self, "_capture_client", None)
        if capture_client is not None:
            return capture_client
        now = time.monotonic()
        if not force and now < getattr(self, "_next_capture_connect_monotonic", 0.0):
            raise RuntimeError("Digitizer reconnection is waiting for its retry interval.")
        host = os.environ.get("EUV_ACQUISITION_HOST", "127.0.0.1")
        control_port = int(os.environ.get("EUV_ACQUISITION_CONTROL_PORT", "11760"))
        artifact_port = int(os.environ.get("EUV_ACQUISITION_ARTIFACT_PORT", "11761"))
        self._log("Connecting to digitizer service.", event="digitizer_connect_started", host=host, control_port=control_port, artifact_port=artifact_port)
        capture_client = AcquisitionClient((host, control_port), (host, artifact_port))
        try:
            capture_client.connect()
            initial_status = capture_client.command("status")
        except Exception as exc:
            capture_client.close()
            self._next_capture_connect_monotonic = now + self.CAPTURE_RECONNECT_DELAY_SECONDS
            self._log(f"Digitizer connection failed: {type(exc).__name__}: {exc}", level="ERROR", event="digitizer_connect_failed", host=host, control_port=control_port, artifact_port=artifact_port)
            raise
        self._capture_client = capture_client
        self._temporary_directory = Path(tempfile.mkdtemp(prefix="euv-acquisition-import-"))
        self._artifact_importer = AcquisitionArtifactImporter(capture_client, self._temporary_directory)
        self._cache_board_status(initial_status)
        self._next_capture_connect_monotonic = 0.0
        try:
            self._cleanup_stale_diagnostic_after_connect(initial_status)
        except Exception as exc:
            self._discard_capture_client(
                f"Could not clean a retained diagnostic session after connecting: {type(exc).__name__}: {exc}"
            )
            raise
        self._log("Connected to digitizer service.", event="digitizer_connect_completed", host=host, control_port=control_port, artifact_port=artifact_port)
        return capture_client

    def _cleanup_stale_diagnostic_after_connect(self, status: dict) -> None:
        raw_session = status.get("session") if isinstance(status, dict) else None
        if raw_session is None:
            return
        session = CaptureSessionManifest.from_dict(raw_session)
        if session.purpose is not CapturePurpose.DIAGNOSTIC:
            return
        with self._run_lock:
            unowned = (
                self._run is None
                and self._diagnostic is None
                and not self._diagnostic_start_pending
            )
        if not unowned:
            return
        self._require_board_capabilities(status, "discard_diagnostic_session")
        self._log(
            "Cleaning retained diagnostic session discovered on digitizer connection.",
            level="WARNING",
            event="acquisition_stale_diagnostic_cleanup_started",
            session_id=str(session.session_id),
            session_state=session.state.value,
        )
        self._discard_retained_diagnostic(session, capture_active=bool(status.get("capture_active")))
        self._cache_board_status(self._capture_command("status", {}))

    def _maintain_capture_connection(self) -> None:
        capture_client = self._capture_client
        if capture_client is None:
            try:
                capture_client = self._ensure_capture_client()
            except Exception:
                return
        try:
            capture_client.heartbeat_if_due()
        except Exception as exc:
            active_run = self._run is not None and not self._run.finalizing
            active_diagnostic = self._diagnostic is not None
            detail = f"Persistent digitizer connection failed: {type(exc).__name__}: {exc}"
            self._discard_capture_client(
                detail
            )
            if active_run:
                self._request_exposure_stop("Persistent digitizer connection failed during an exposure.")
            elif active_diagnostic:
                self._fail_diagnostic(detail)

    def _capture_command(self, command: str, payload: dict) -> dict:
        if self._capture_client is None:
            raise RuntimeError("Digitizer client is not connected.")
        started_at = time.monotonic()
        self._log(f"Sending digitizer command {command}.", level="DEBUG", event="digitizer_command_sent", command=command, payload=payload)
        try:
            result = self._capture_client.command(command, payload)
        except Exception as exc:
            self._log(f"Digitizer command {command} failed after {time.monotonic() - started_at:.3f}s: {type(exc).__name__}: {exc}", level="ERROR", event="digitizer_command_failed", command=command, payload=payload)
            if isinstance(exc, (ConnectionError, OSError, TimeoutError)):
                self._discard_capture_client(
                    f"Digitizer transport failed while running {command}: {type(exc).__name__}: {exc}"
                )
            raise
        self._log(f"Digitizer command {command} completed in {time.monotonic() - started_at:.3f}s.", level="DEBUG", event="digitizer_command_completed", command=command, result=result)
        self._cache_board_status(result)
        return result

    def _cache_board_status(self, result: dict) -> None:
        if not isinstance(result, dict):
            return
        if "source_kind" in result and "capabilities" in result:
            self._board_status = dict(result)
        elif "simulator" in result:
            self._board_status = dict(getattr(self, "_board_status", {}))
            self._board_status["simulator"] = result["simulator"]

    def _close_capture_client(self) -> None:
        if self._capture_client is not None:
            self._log("Closing digitizer service connection.", event="digitizer_connection_closing")
            self._capture_client.close()
        self._capture_client = None
        self._artifact_importer = None
        temporary_directory = getattr(self, "_temporary_directory", None)
        self._temporary_directory = None
        if temporary_directory is not None:
            shutil.rmtree(temporary_directory, ignore_errors=True)

    def _discard_capture_client(self, reason: str) -> None:
        if self._capture_client is not None:
            self._log(reason, level="WARNING", event="digitizer_connection_lost")
            close = getattr(self._capture_client, "close", None)
            if close is not None:
                close()
        self._capture_client = None
        self._artifact_importer = None
        temporary_directory = getattr(self, "_temporary_directory", None)
        self._temporary_directory = None
        if temporary_directory is not None:
            shutil.rmtree(temporary_directory, ignore_errors=True)
        self._next_capture_connect_monotonic = time.monotonic() + self.CAPTURE_RECONNECT_DELAY_SECONDS

    def _recover_orphaned_capture_session(self) -> str:
        if self._reader is None:
            raise RuntimeError("Experiment reader is not available.")
        capture_client = self._ensure_capture_client(force=True)
        self._log("Reading digitizer spool status for orphan recovery.", event="orphan_recovery_status_read")
        status = self._capture_command("status", {})
        raw_session = status.get("session")
        if raw_session is None:
            self._log("Orphan recovery found no unreleased digitizer session.", level="WARNING", event="orphan_recovery_no_session")
            raise RuntimeError("The digitizer spool has no unreleased capture session.")
        session = CaptureSessionManifest.from_dict(raw_session)
        self._log("Located recoverable digitizer session.", event="orphan_recovery_session_found", session_id=str(session.session_id), session_state=session.state.value, snapshot_count=len(session.snapshots))
        if session.state is not CaptureSessionState.ORPHANED:
            if session.state is not CaptureSessionState.STOPPED:
                raise RuntimeError(f"The digitizer session is {session.state.value}, not recoverable.")
        record = self._find_record_for_capture_session(session.session_id)
        run_id = record.get_state().get_uuid()
        self._log("Located exposure record for orphaned capture recovery.", event="orphan_recovery_record_found", session_id=str(session.session_id), run_id=str(run_id))
        entry = record.get_record()
        recovery_directory = Path(tempfile.mkdtemp(prefix="euv-acquisition-recovery-"))
        importer = AcquisitionArtifactImporter(capture_client, recovery_directory)
        imported = 0
        try:
            for stored_snapshot in session.snapshots:
                if not stored_snapshot.acknowledged:
                    manifest = stored_snapshot.manifest
                    self._log("Recovering unacknowledged digitizer snapshot.", event="orphan_recovery_snapshot_import", session_id=str(session.session_id), snapshot_id=str(manifest.snapshot_id))
                    temporary_paths = {recovery_directory / manifest.filename}
                    try:
                        path = importer.import_snapshot(
                            record,
                            manifest,
                            after_persist=lambda _entry, local_path: self._publish_experiment_preview(
                                temporary_paths,
                                local_path,
                                manifest,
                                run_id,
                            ),
                        )
                        temporary_paths.add(path)
                    finally:
                        for temporary_path in temporary_paths:
                            temporary_path.unlink(missing_ok=True)
                    imported += 1
                    self._log("Recalculating dose analysis for recovered capture session.", event="orphan_recovery_analysis_started", session_id=str(session.session_id), imported_snapshot_count=imported)
            result = analyze_experiment_entry(run_id, entry)
            write_analysis_revision(
                entry,
                DoseAnalysisRevision(uuid.uuid4(), time.time(), result),
                promote=True,
            )
            self._ensure_persisted_exposure_graph(run_id, entry, context="orphaned capture recovery")
            self._log("Reconciled recovered capture analysis; releasing digitizer spool.", event="orphan_recovery_release_started", session_id=str(session.session_id))
            self._capture_command("release_snapshots", {})
            self._deferred_finalization_detail = None
            self._log("Completed orphaned capture recovery and released the digitizer spool.", event="orphan_recovery_completed", session_id=str(session.session_id), imported_snapshot_count=imported)
            return (
                f"Recovered orphaned capture session {session.session_id}: imported {imported} snapshot(s), "
                "reconciled dose analysis, and released the digitizer spool."
            )
        finally:
            shutil.rmtree(recovery_directory, ignore_errors=True)

    def _find_record_for_capture_session(self, session_id: uuid.UUID):
        if self._reader is None:
            raise RuntimeError("Experiment reader is not available.")
        matches = self._reader.list_runs(q_tags={"euv_capture_session_id": str(session_id)})
        if not matches:
            for record in self._reader.list_runs():
                entry = record.get_record()
                resources = dict(entry.list_resources())
                if CAPTURE_SESSION_RESOURCE not in resources:
                    continue
                with entry.resource(CAPTURE_SESSION_RESOURCE, "euv_capture_session", "r") as resource:
                    provenance = json.load(resource)
                if provenance.get("session_id") == str(session_id):
                    matches.append(record)
        if len(matches) != 1:
            raise RuntimeError(
                f"Expected one exposure record for orphaned capture session {session_id}, found {len(matches)}."
            )
        return matches[0]

    def _get_record(self, run_id: uuid.UUID):
        if self._reader is None:
            raise RuntimeError("Experiment reader is not available.")
        return self._reader.get_run(run_id)

    def _write_preinit_provenance(self, session_id: uuid.UUID, run: _AcquisitionRun) -> None:
        reader = ExperimentReader(self._data_path, "exposure")
        try:
            record = reader.get_run(run.run_id)
            write_capture_provenance(
                record.get_record(),
                session_id,
                run.calibration,
                run.chopper_frequency_hz,
            )
        finally:
            reader.close()

    def _require_run(self) -> _AcquisitionRun:
        if self._run is None:
            raise RuntimeError("No acquisition exposure is active.")
        return self._run

    def close(self) -> None:
        self._daemon.stop()
        self._close_capture_client()
        self._run_event_emitter.flush(5.0)
        self._run_event_emitter.close()
        self._dds_client.close()
        self._logger_socket.close()

    def ok(self) -> bool:
        return self._daemon.is_ok() and self._dds_client.ok()


def main(stop_event) -> None:
    subsystem = EuvAcquisitionSubsystem()
    try:
        while subsystem.ok() and not stop_event.is_set():
            time.sleep(0.1)
    finally:
        subsystem.close()