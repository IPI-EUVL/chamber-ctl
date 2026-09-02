import queue
import threading
import uuid
from dataclasses import replace

from ipi_ecs.subsystems.experiment_controller import ExperimentController
from ipi_ecs.subsystems.experiment_controller import RunState
import pytest
import segment_bytes

from chamber_ctl.data.calibration import SourceCalibrationBinding, SourceKey
from chamber_ctl.subsystems.exposure_controller import ExposureSettings
from chamber_ctl.subsystems.siglent_observer import (
    ObserverCaptureRun,
    ObserverRecoveryJournal,
    SiglentObserverDdsAdapter,
    SiglentObserverCoordinator,
    SiglentObserverService,
    _parse_args,
    decode_observer_exposure_state,
    observer_subsystem_uuid,
)
from chamber_ctl.subsystems import uuids
from euv_acquisition.session import CaptureSessionManifest, CaptureSessionState
from euv_acquisition.timing import LaserTimingState


SOURCE = SourceKey("siglent", "default")
RUN_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
SESSION_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
BINDING = SourceCalibrationBinding(SOURCE.source_kind, SOURCE.source_id, uuid.uuid4(), 3)


def _state_payload(phase: int, settings: ExposureSettings | None = None) -> bytes:
    run_payload = b""
    if settings is not None:
        run_payload = RunState("exposure", settings, RUN_ID).encode().encode("utf-8")
    return segment_bytes.encode([phase.to_bytes(1, "big"), run_payload])


def test_observer_state_decoder_selects_only_its_source_binding() -> None:
    legacy_settings = ExposureSettings()
    legacy_settings.data["source_calibrations"] = [BINDING.to_dict()]
    state = decode_observer_exposure_state(
        _state_payload(
            ExperimentController.RUN_STATE_PREINIT,
            legacy_settings,
        ),
        SOURCE,
    )
    disabled = decode_observer_exposure_state(
        _state_payload(ExperimentController.RUN_STATE_PREINIT, ExposureSettings()),
        SOURCE,
    )
    stopped = decode_observer_exposure_state(
        _state_payload(ExperimentController.RUN_STATE_STOPPED),
        SOURCE,
    )

    assert (state.phase, state.run_id, state.calibration) == (
        ExperimentController.RUN_STATE_PREINIT,
        RUN_ID,
        BINDING,
    )
    assert disabled.calibration is None
    assert stopped.run_id is None


def test_observer_state_decoder_uses_fallback_only_when_run_binding_is_absent() -> None:
    run_binding = replace(BINDING, revision=4)
    legacy_settings = ExposureSettings()
    legacy_settings.data["source_calibrations"] = [run_binding.to_dict()]
    fallback_state = decode_observer_exposure_state(
        _state_payload(ExperimentController.RUN_STATE_PREINIT, ExposureSettings()),
        SOURCE,
        BINDING,
    )
    explicit_state = decode_observer_exposure_state(
        _state_payload(
            ExperimentController.RUN_STATE_PREINIT,
            legacy_settings,
        ),
        SOURCE,
        BINDING,
    )

    assert fallback_state.calibration == BINDING
    assert explicit_state.calibration == run_binding


def test_observer_state_decoder_rejects_fallback_for_another_source() -> None:
    with pytest.raises(ValueError, match="another source"):
        decode_observer_exposure_state(
            _state_payload(ExperimentController.RUN_STATE_PREINIT, ExposureSettings()),
            SOURCE,
            replace(BINDING, source_id="other"),
        )


def test_observer_state_decoder_rejects_malformed_active_state() -> None:
    with pytest.raises(ValueError, match="omitted its run payload"):
        decode_observer_exposure_state(
            _state_payload(ExperimentController.RUN_STATE_RUNNING),
            SOURCE,
        )


class _RemoteKv:
    def __init__(self) -> None:
        self.callback = None

    def on_new_data_received(self, callback) -> None:
        self.callback = callback


class _DdsHandle:
    def __init__(self) -> None:
        self.remotes = []

    def add_remote_kv(self, subsystem_id, descriptor):
        remote = _RemoteKv()
        self.remotes.append((subsystem_id, descriptor, remote))
        return remote


def test_dds_adapter_registers_only_read_only_remote_kvs() -> None:
    exposure_payloads = []
    timing_payloads = []
    handle = _DdsHandle()
    adapter = SiglentObserverDdsAdapter(exposure_payloads.append, timing_payloads.append)

    adapter.configure(handle)

    assert [(target, descriptor.get_key()) for target, descriptor, _remote in handle.remotes] == [
        (uuids.UUID_EXPOSURE_CONTROLLER, b"experiment_state"),
        (uuids.UUID_LASER_CONTROLLER, b"timing_status"),
    ]
    assert all(descriptor.get_writable() is False for _target, descriptor, _remote in handle.remotes)
    handle.remotes[0][2].callback(b"exposure")
    handle.remotes[1][2].callback(b"timing")
    assert exposure_payloads == [b"exposure"]
    assert timing_payloads == [b"timing"]


def test_observer_cli_uses_independent_siglent_ports() -> None:
    args = _parse_args(["--source-id", "scope-1"])

    assert args.control_port == 11762
    assert args.artifact_port == 11763
    assert args.source_kind == "siglent"


def test_observer_subsystem_identity_is_stable_and_source_qualified() -> None:
    assert observer_subsystem_uuid(SOURCE) == observer_subsystem_uuid(SourceKey("siglent", "default"))
    assert observer_subsystem_uuid(SOURCE) != observer_subsystem_uuid(SourceKey("siglent", "other"))


def test_observer_cli_requires_a_complete_fallback_calibration() -> None:
    with pytest.raises(SystemExit):
        _parse_args(["--source-id", "scope-1", "--calibration-profile-id", str(BINDING.profile_id)])

    args = _parse_args(
        [
            "--source-id",
            "scope-1",
            "--calibration-profile-id",
            str(BINDING.profile_id),
            "--calibration-revision",
            "3",
        ]
    )
    assert args.calibration_revision == 3


def test_observer_recovery_journal_resumes_only_the_mapped_session(tmp_path) -> None:
    journal = ObserverRecoveryJournal(tmp_path / "observer.json")
    run = ObserverCaptureRun(RUN_ID, SESSION_ID, SOURCE, BINDING, 10, 20)
    journal.save(run)
    client = _Client()
    client.session = CaptureSessionManifest(
        server_boot_id=uuid.uuid4(),
        session_id=SESSION_ID,
        state=CaptureSessionState.ACTIVE,
        source_kind=SOURCE.source_kind,
        source_id=SOURCE.source_id,
        started_at_unix_ns=10,
    )
    prepared = []
    finalized = []
    coordinator = SiglentObserverCoordinator(
        SOURCE,
        lambda: client,
        lambda recovered, _client: prepared.append(recovered),
        lambda recovered, manifest, _client: finalized.append((recovered, manifest)),
        now_ns=lambda: 25,
        journal=journal,
    )

    coordinator.recover()
    coordinator.observe_phase(ExperimentController.RUN_STATE_STOPPED)

    assert len(prepared) == 1
    assert len(finalized) == 1
    assert [command for command, _payload in client.commands].count("start_capture") == 0
    assert client.released is True
    assert journal.load() is None


def test_observer_recovery_refuses_a_different_retained_session(tmp_path) -> None:
    journal = ObserverRecoveryJournal(tmp_path / "observer.json")
    journal.save(ObserverCaptureRun(RUN_ID, SESSION_ID, SOURCE, BINDING, 10))
    client = _Client()
    client.session = CaptureSessionManifest(
        server_boot_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        state=CaptureSessionState.STOPPED,
        source_kind=SOURCE.source_kind,
        source_id=SOURCE.source_id,
        started_at_unix_ns=10,
        stop_reason="other run",
    )
    finalized = []
    coordinator = SiglentObserverCoordinator(
        SOURCE,
        lambda: client,
        lambda _run, _client: None,
        lambda run, manifest, _client: finalized.append((run, manifest)),
        journal=journal,
    )

    coordinator.recover()

    assert coordinator.current_run is None
    assert finalized == []
    assert client.released is False
    assert journal.load().session_id == SESSION_ID
    assert "wrong capture session" in coordinator.last_error


class _Client:
    def __init__(self, *, start_error: Exception | None = None) -> None:
        self.start_error = start_error
        self.connected = False
        self.closed = False
        self.released = False
        self.commands = []
        self.session = None
        self.stop_reasons = queue.Queue()

    def connect(self) -> None:
        self.connected = True

    def close(self) -> None:
        self.closed = True

    def heartbeat_if_due(self) -> bool:
        return True

    def get_stop_reason(self, timeout=None) -> str:
        return self.stop_reasons.get(timeout=timeout)

    def command(self, command, payload=None):
        self.commands.append((command, payload))
        if command == "status":
            return self._status()
        if command == "start_capture":
            if self.start_error is not None:
                raise self.start_error
            self.session = CaptureSessionManifest(
                server_boot_id=uuid.uuid4(),
                session_id=uuid.UUID(payload["session_id"]),
                state=CaptureSessionState.ACTIVE,
                source_kind=SOURCE.source_kind,
                source_id=SOURCE.source_id,
                started_at_unix_ns=10,
            )
            return self._status()
        if command == "stop_capture":
            self.session = replace(
                self.session,
                state=CaptureSessionState.STOPPED,
                stop_reason=payload["reason"],
            )
            return self._status()
        if command == "release_snapshots":
            self.released = True
            self.session = None
            return {"released": True}
        raise AssertionError(command)

    def _status(self):
        return {
            "source_kind": SOURCE.source_kind,
            "source_id": SOURCE.source_id,
            "capture_active": self.session is not None and self.session.state is CaptureSessionState.ACTIVE,
            "session": None if self.session is None else self.session.to_dict(),
        }


def test_observer_starts_at_preinit_records_running_and_finalizes_after_stopped() -> None:
    client = _Client()
    prepared = []
    finalized = []
    times = iter((100, 200))
    coordinator = SiglentObserverCoordinator(
        SOURCE,
        lambda: client,
        lambda run, _client: prepared.append(run),
        lambda run, manifest, _client: finalized.append((run, manifest)),
        now_ns=lambda: next(times),
        session_id_factory=lambda: SESSION_ID,
    )

    coordinator.observe_phase(
        ExperimentController.RUN_STATE_PREINIT,
        run_id=RUN_ID,
        calibration=BINDING,
    )
    coordinator.observe_phase(
        ExperimentController.RUN_STATE_PREINIT,
        run_id=RUN_ID,
        calibration=BINDING,
    )
    coordinator.observe_phase(ExperimentController.RUN_STATE_RUNNING, run_id=RUN_ID)
    coordinator.observe_phase(ExperimentController.RUN_STATE_RUNNING, run_id=RUN_ID)
    coordinator.observe_timing(
        LaserTimingState(
            laser_on=True,
            laser_warming_up=False,
            chopper_on=True,
            chopper_starting_up=False,
            current_phase=0.5,
            preinit_phase=0.0,
            configured_target_phase=0.5,
            chopper_frequency_hz=192.0,
            sampled_at_unix_ns=150,
        )
    )
    coordinator.observe_phase(ExperimentController.RUN_STATE_STOPPED)

    assert len(prepared) == 1
    assert len(finalized) == 1
    finalized_run, manifest = finalized[0]
    assert finalized_run.exposure_started_unix_ns == 100
    assert finalized_run.stopped_observed_unix_ns == 200
    assert len(finalized_run.timing_observations) == 1
    assert finalized_run.timing_observations[0].transmitting is True
    assert manifest.state is CaptureSessionState.STOPPED
    assert [command for command, _payload in client.commands].count("start_capture") == 1
    assert client.released is True
    assert client.closed is True
    assert coordinator.current_run is None
    assert coordinator.last_error is None


def test_observer_start_failure_is_contained() -> None:
    client = _Client(start_error=RuntimeError("scope unavailable"))
    coordinator = SiglentObserverCoordinator(
        SOURCE,
        lambda: client,
        lambda _run, _client: None,
        lambda _run, _manifest, _client: None,
        session_id_factory=lambda: SESSION_ID,
    )

    coordinator.observe_phase(
        ExperimentController.RUN_STATE_PREINIT,
        run_id=RUN_ID,
        calibration=BINDING,
    )

    assert coordinator.current_run is None
    assert "scope unavailable" in coordinator.last_error
    assert client.closed is True


def test_observer_does_not_release_spool_when_record_finalization_fails() -> None:
    client = _Client()
    coordinator = SiglentObserverCoordinator(
        SOURCE,
        lambda: client,
        lambda _run, _client: None,
        lambda _run, _manifest, _client: (_ for _ in ()).throw(RuntimeError("database unavailable")),
        now_ns=lambda: 100,
        session_id_factory=lambda: SESSION_ID,
    )
    coordinator.observe_phase(
        ExperimentController.RUN_STATE_PREINIT,
        run_id=RUN_ID,
        calibration=BINDING,
    )

    coordinator.observe_phase(ExperimentController.RUN_STATE_STOPPED)

    assert coordinator.current_run is not None
    assert client.released is False
    assert client.closed is False
    assert "database unavailable" in coordinator.last_error


def test_observer_reports_and_preserves_an_unexpected_source_stop() -> None:
    client = _Client()
    finalized = []
    coordinator = SiglentObserverCoordinator(
        SOURCE,
        lambda: client,
        lambda _run, _client: None,
        lambda run, manifest, _client: finalized.append((run, manifest)),
        session_id_factory=lambda: SESSION_ID,
    )
    coordinator.observe_phase(
        ExperimentController.RUN_STATE_PREINIT,
        run_id=RUN_ID,
        calibration=BINDING,
    )
    failure = "Pulse source failure: ValueError: invalid preamble"
    client.session = replace(
        client.session,
        state=CaptureSessionState.STOPPED,
        stop_reason=failure,
    )
    client.stop_reasons.put(failure)

    coordinator.heartbeat()
    coordinator.observe_phase(ExperimentController.RUN_STATE_STOPPED)

    assert coordinator.current_run is not None
    assert finalized == []
    assert client.released is False
    assert failure in coordinator.last_error


class _StatusHandle:
    def __init__(self) -> None:
        self.items = {}

    def put_status_item(self, item) -> None:
        self.items[item.get_code()] = item

    def get_status_item_exists(self, code: int) -> bool:
        return code in self.items

    def clear_status_item(self, code: int) -> None:
        self.items.pop(code, None)


def test_observer_publishes_coordinator_errors_as_dds_alarms() -> None:
    coordinator = SiglentObserverCoordinator(
        SOURCE,
        lambda: None,
        lambda _run, _client: None,
        lambda _run, _manifest, _client: None,
    )
    coordinator._record_error("scope capture failed")
    handle = _StatusHandle()
    service = object.__new__(SiglentObserverService)
    service._coordinator = coordinator
    service._status_lock = threading.Lock()
    service._subsystem_handle = handle
    service._published_health = None

    service._publish_status()

    assert handle.items[0].get_message() == "Idle"
    assert handle.items[100].get_severity() == handle.items[100].STATE_ALARM
    assert handle.items[100].get_message() == "scope capture failed"