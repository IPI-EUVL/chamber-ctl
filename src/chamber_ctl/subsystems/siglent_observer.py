from __future__ import annotations

import argparse
import json
import os
import queue
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

import ipi_ecs.dds.client as dds_client
import ipi_ecs.dds.subsystem as dds_subsystem
import ipi_ecs.dds.types as dds_types
from ipi_ecs.subsystems.experiment_controller import ExperimentController
from ipi_ecs.subsystems.experiment_controller import RunState
import segment_bytes

from chamber_ctl.data.observer_artifacts import ObserverArtifactRecorder
from chamber_ctl.data.calibration import (
    SourceCalibrationBinding,
    SourceKey,
    normalize_source_calibration_bindings,
)
from chamber_ctl.subsystems import uuids
from euv_acquisition.ecs_logging import open_ecs_logger
from euv_acquisition.service import AcquisitionClient
from euv_acquisition.session import (
    CapturePurpose,
    CaptureSessionManifest,
    CaptureSessionState,
)
from euv_acquisition.timing import LaserTimingState


@dataclass(frozen=True)
class ObserverTimingObservation:
    sampled_at_unix_ns: int
    transmitting: bool
    clock_basis: str


@dataclass(frozen=True)
class ObserverCaptureRun:
    run_id: uuid.UUID
    session_id: uuid.UUID
    source_key: SourceKey
    calibration: SourceCalibrationBinding
    capture_started_unix_ns: int
    exposure_started_unix_ns: int | None = None
    stopped_observed_unix_ns: int | None = None
    timing_observations: tuple[ObserverTimingObservation, ...] = ()
    timing_evidence_started_unix_ns: int | None = None


class ObserverRecoveryJournal:
    SCHEMA_VERSION = 1

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> ObserverCaptureRun | None:
        try:
            with self.path.open("r", encoding="utf-8") as source:
                value = json.load(source)
        except FileNotFoundError:
            return None
        expected = {
            "schema_version",
            "run_id",
            "session_id",
            "source_kind",
            "source_id",
            "calibration",
            "capture_started_unix_ns",
            "exposure_started_unix_ns",
            "stopped_observed_unix_ns",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("Observer recovery journal contains unknown or missing fields.")
        if value["schema_version"] != self.SCHEMA_VERSION:
            raise ValueError("Observer recovery journal has an unsupported schema version.")
        calibration = SourceCalibrationBinding.from_dict(value["calibration"])
        run = ObserverCaptureRun(
            run_id=uuid.UUID(str(value["run_id"])),
            session_id=uuid.UUID(str(value["session_id"])),
            source_key=SourceKey(value["source_kind"], value["source_id"]),
            calibration=calibration,
            capture_started_unix_ns=self._optional_non_negative_int(
                value["capture_started_unix_ns"],
                "capture_started_unix_ns",
                required=True,
            ),
            exposure_started_unix_ns=self._optional_non_negative_int(
                value["exposure_started_unix_ns"],
                "exposure_started_unix_ns",
            ),
            stopped_observed_unix_ns=self._optional_non_negative_int(
                value["stopped_observed_unix_ns"],
                "stopped_observed_unix_ns",
            ),
        )
        if calibration.source_key != run.source_key:
            raise ValueError("Observer recovery calibration belongs to another source.")
        return run

    def save(self, run: ObserverCaptureRun) -> None:
        value = {
            "schema_version": self.SCHEMA_VERSION,
            "run_id": str(run.run_id),
            "session_id": str(run.session_id),
            "source_kind": run.source_key.source_kind,
            "source_id": run.source_key.source_id,
            "calibration": run.calibration.to_dict(),
            "capture_started_unix_ns": run.capture_started_unix_ns,
            "exposure_started_unix_ns": run.exposure_started_unix_ns,
            "stopped_observed_unix_ns": run.stopped_observed_unix_ns,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as output:
                json.dump(value, output, allow_nan=False, sort_keys=True, separators=(",", ":"))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)

    @staticmethod
    def _optional_non_negative_int(value, name: str, *, required: bool = False) -> int | None:
        if value is None and not required:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"Observer recovery {name} must be a non-negative integer.")
        return value


class _NullObserverRecoveryJournal:
    def load(self) -> None:
        return None

    def save(self, _run: ObserverCaptureRun) -> None:
        pass

    def clear(self) -> None:
        pass


@dataclass(frozen=True)
class ObserverExposureState:
    phase: int
    run_id: uuid.UUID | None
    calibration: SourceCalibrationBinding | None


def decode_observer_exposure_state(
    payload: bytes,
    source_key: SourceKey,
    fallback_calibration: SourceCalibrationBinding | None = None,
) -> ObserverExposureState:
    if fallback_calibration is not None and fallback_calibration.source_key != source_key:
        raise ValueError("Observer fallback calibration belongs to another source.")
    parts = segment_bytes.decode(payload)
    if len(parts) != 2 or len(parts[0]) != 1:
        raise ValueError("Exposure state must contain a one-byte phase and run payload.")
    phase = int.from_bytes(parts[0], byteorder="big")
    if phase not in ExperimentController.RUN_STATE_NAMES:
        raise ValueError(f"Exposure state contains unknown phase {phase}.")
    if phase == ExperimentController.RUN_STATE_STOPPED and not parts[1]:
        return ObserverExposureState(phase, None, None)
    if not parts[1]:
        raise ValueError("Active exposure state omitted its run payload.")
    try:
        run = RunState.decode(parts[1].decode("utf-8"))
    except (UnicodeDecodeError, ValueError, TypeError, KeyError) as exc:
        raise ValueError("Exposure state contains an invalid run payload.") from exc
    if run.get_type() != "exposure":
        raise ValueError("Observer received a non-exposure run state.")
    bindings = normalize_source_calibration_bindings(
        run.get_settings().get_dict().get("source_calibrations", [])
    )
    calibration = next(
        (binding for binding in bindings if binding.source_key == source_key),
        fallback_calibration,
    )
    return ObserverExposureState(phase, run.get_uuid(), calibration)


class SiglentObserverCoordinator:
    def __init__(
        self,
        source_key: SourceKey,
        client_factory: Callable[[], object],
        prepare_run: Callable[[ObserverCaptureRun, object], None],
        finalize_run: Callable[[ObserverCaptureRun, CaptureSessionManifest, object], None],
        *,
        now_ns: Callable[[], int] = time.time_ns,
        session_id_factory: Callable[[], uuid.UUID] = uuid.uuid4,
        log: Callable[[str, str], None] | None = None,
        journal: ObserverRecoveryJournal | None = None,
    ) -> None:
        self.source_key = source_key
        self._client_factory = client_factory
        self._prepare_run = prepare_run
        self._finalize_run = finalize_run
        self._now_ns = now_ns
        self._session_id_factory = session_id_factory
        self._log = log or (lambda _message, _level: None)
        self._journal = journal or _NullObserverRecoveryJournal()
        self._lock = threading.RLock()
        self._client = None
        self._run: ObserverCaptureRun | None = None
        self._timing_observations: list[ObserverTimingObservation] = []
        self._last_error: str | None = None

    @property
    def current_run(self) -> ObserverCaptureRun | None:
        with self._lock:
            return self._run

    @property
    def last_error(self) -> str | None:
        with self._lock:
            return self._last_error

    def observe_phase(
        self,
        phase: int,
        *,
        run_id: uuid.UUID | None = None,
        calibration: SourceCalibrationBinding | None = None,
    ) -> None:
        try:
            with self._lock:
                if phase == ExperimentController.RUN_STATE_PREINIT:
                    if run_id is not None and calibration is not None:
                        self._start(run_id, calibration)
                elif phase == ExperimentController.RUN_STATE_RUNNING:
                    self._mark_running(run_id)
                elif phase == ExperimentController.RUN_STATE_STOPPED:
                    self._finalize()
        except Exception as exc:
            self._record_error(f"{type(exc).__name__}: {exc}")

    def heartbeat(self) -> None:
        with self._lock:
            client = self._client
        if client is None:
            return
        try:
            client.heartbeat_if_due()
        except Exception as exc:
            self._record_error(f"Observer heartbeat failed: {type(exc).__name__}: {exc}")

    def recover(self) -> None:
        try:
            with self._lock:
                self._recover()
        except Exception as exc:
            self._record_error(f"Observer recovery failed: {type(exc).__name__}: {exc}")

    def observe_timing(self, state: LaserTimingState) -> None:
        with self._lock:
            if self._run is None or self._run.stopped_observed_unix_ns is not None:
                return
            sampled_at = state.sampled_at_unix_ns
            basis = "laser_status"
            if sampled_at is None:
                sampled_at = self._now_ns()
                basis = "observer_receive"
            observation = ObserverTimingObservation(
                sampled_at_unix_ns=sampled_at,
                transmitting=state.euv_transmitting(),
                clock_basis=basis,
            )
            if not self._timing_observations or self._timing_observations[-1] != observation:
                self._timing_observations.append(observation)

    def close(self) -> None:
        try:
            with self._lock:
                self._finalize()
        except Exception as exc:
            self._record_error(f"Observer shutdown finalization failed: {type(exc).__name__}: {exc}")
        finally:
            with self._lock:
                self._close_client()

    def _start(self, run_id: uuid.UUID, calibration: SourceCalibrationBinding) -> None:
        if calibration.source_key != self.source_key:
            raise ValueError("Observer calibration belongs to another capture source.")
        if self._run is not None:
            if self._run.run_id == run_id:
                return
            raise RuntimeError(
                f"Observer run {self._run.run_id} is still pending finalization; cannot start {run_id}."
            )

        pending = self._journal.load()
        if pending is not None:
            if pending.run_id != run_id or pending.calibration != calibration:
                raise RuntimeError("Observer recovery journal belongs to another exposure or calibration.")
            self._recover()
            if self._run is not None:
                return

        client = self._client_factory()
        session_id = self._session_id_factory()
        provisional_run = ObserverCaptureRun(
            run_id=run_id,
            session_id=session_id,
            source_key=self.source_key,
            calibration=calibration,
            capture_started_unix_ns=0,
        )
        self._journal.save(provisional_run)
        try:
            client.connect()
            status = client.command("status")
            self._validate_idle_status(status)
            started = client.command("start_capture", {"session_id": str(session_id)})
            manifest = self._session_from_status(started)
            self._validate_session(manifest, session_id, active=True)
            run = ObserverCaptureRun(
                run_id=run_id,
                session_id=session_id,
                source_key=self.source_key,
                calibration=calibration,
                capture_started_unix_ns=manifest.started_at_unix_ns,
                timing_evidence_started_unix_ns=manifest.started_at_unix_ns,
            )
            self._journal.save(run)
            self._prepare_run(run, client)
        except Exception:
            try:
                client.command("stop_capture", {"reason": "Observer start did not complete."})
            except Exception:
                pass
            try:
                status = client.command("status")
                if isinstance(status, dict) and status.get("session") is None:
                    self._journal.clear()
            except Exception:
                pass
            try:
                client.close()
            except Exception:
                pass
            raise

        self._client = client
        self._run = run
        self._timing_observations = []
        self._last_error = None
        self._log(f"Started passive capture session {session_id} for exposure {run_id}.", "INFO")

    def _mark_running(self, run_id: uuid.UUID | None) -> None:
        if self._run is None or self._run.run_id != run_id:
            return
        if self._run.exposure_started_unix_ns is None:
            self._run = replace(self._run, exposure_started_unix_ns=self._now_ns())
            self._journal.save(self._run)

    def _finalize(self) -> None:
        run = self._run
        client = self._client
        if run is None or client is None:
            return
        if run.stopped_observed_unix_ns is None:
            run = replace(
                run,
                stopped_observed_unix_ns=self._now_ns(),
                timing_observations=tuple(self._timing_observations),
            )
            self._run = run
            self._journal.save(run)

        status = client.command("status")
        if bool(status.get("capture_active")):
            status = client.command(
                "stop_capture",
                {"reason": f"Exposure {run.run_id} reached STOPPED."},
            )
        manifest = self._session_from_status(status)
        self._validate_session(manifest, run.session_id, active=False)
        self._finalize_run(run, manifest, client)
        client.command("release_snapshots", {})
        self._journal.clear()
        self._close_client()
        self._run = None
        self._timing_observations = []
        self._last_error = None
        self._log(f"Finalized passive capture session {run.session_id}.", "INFO")

    def _recover(self) -> None:
        if self._run is not None:
            return
        run = self._journal.load()
        if run is None:
            return
        if run.source_key != self.source_key:
            raise ValueError("Observer recovery journal belongs to another source.")
        client = self._client_factory()
        try:
            client.connect()
            status = client.command("status")
            self._validate_status_source(status)
            manifest = self._session_from_status(status)
            self._validate_session(
                manifest,
                run.session_id,
                active=manifest.state is CaptureSessionState.ACTIVE,
            )
            run = replace(
                run,
                capture_started_unix_ns=manifest.started_at_unix_ns,
                timing_evidence_started_unix_ns=self._now_ns(),
            )
            self._journal.save(run)
            self._prepare_run(run, client)
        except Exception:
            try:
                client.close()
            except Exception:
                pass
            raise
        self._client = client
        self._run = run
        self._timing_observations = []
        self._last_error = None
        if manifest.state is CaptureSessionState.ACTIVE:
            self._log(f"Recovered active passive capture session {run.session_id}.", "INFO")
        else:
            self._finalize()

    def _validate_idle_status(self, status: object) -> None:
        self._validate_status_source(status)
        if status.get("capture_active") is not False:
            raise RuntimeError("Observer acquisition service is already capturing.")
        if status.get("session") is not None:
            raise RuntimeError("Observer acquisition spool contains an unreleased session.")

    def _validate_status_source(self, status: object) -> None:
        if not isinstance(status, dict):
            raise ValueError("Observer acquisition status must be an object.")
        if status.get("source_kind") != self.source_key.source_kind:
            raise ValueError("Observer acquisition service reported the wrong source kind.")
        if status.get("source_id") != self.source_key.source_id:
            raise ValueError("Observer acquisition service reported the wrong source ID.")

    @staticmethod
    def _session_from_status(status: object) -> CaptureSessionManifest:
        if not isinstance(status, dict) or not isinstance(status.get("session"), dict):
            raise ValueError("Observer acquisition status omitted its capture session.")
        return CaptureSessionManifest.from_dict(status["session"])

    def _validate_session(
        self,
        manifest: CaptureSessionManifest,
        session_id: uuid.UUID,
        *,
        active: bool,
    ) -> None:
        if manifest.session_id != session_id:
            raise ValueError("Observer acquisition service returned the wrong capture session.")
        if (manifest.source_kind, manifest.source_id) != (
            self.source_key.source_kind,
            self.source_key.source_id,
        ):
            raise ValueError("Observer capture session belongs to another source.")
        if manifest.purpose is not CapturePurpose.EXPERIMENT:
            raise ValueError("Observer capture session has the wrong purpose.")
        if active and manifest.state is not CaptureSessionState.ACTIVE:
            raise ValueError("New observer capture session is not active.")
        if not active and manifest.state is CaptureSessionState.ACTIVE:
            raise ValueError("Observer capture session did not reach a terminal state.")

    def _record_error(self, message: str) -> None:
        with self._lock:
            self._last_error = message
        self._log(message, "ERROR")

    def _close_client(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            try:
                client.close()
            except Exception as exc:
                self._record_error(f"Observer client close failed: {type(exc).__name__}: {exc}")


class SiglentObserverDdsAdapter:
    def __init__(
        self,
        on_exposure_state: Callable[[bytes], None],
        on_timing_state: Callable[[bytes], None],
    ) -> None:
        self._on_exposure_state = on_exposure_state
        self._on_timing_state = on_timing_state
        self._configured = False

    def configure(self, handle) -> None:
        if self._configured:
            return
        self._configured = True
        exposure_state = handle.add_remote_kv(
            uuids.UUID_EXPOSURE_CONTROLLER,
            dds_subsystem.KVDescriptor(
                dds_types.ByteTypeSpecifier(),
                b"experiment_state",
                True,
                True,
                False,
            ),
        )
        timing_state = handle.add_remote_kv(
            uuids.UUID_LASER_CONTROLLER,
            dds_subsystem.KVDescriptor(
                dds_types.ByteTypeSpecifier(),
                b"timing_status",
                True,
                True,
                False,
            ),
        )
        exposure_state.on_new_data_received(self._on_exposure_state)
        timing_state.on_new_data_received(self._on_timing_state)


def observer_subsystem_uuid(source_key: SourceKey) -> uuid.UUID:
    return uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"euvl/acquisition-observer/{source_key.source_kind}\0{source_key.source_id}",
    )


class SiglentObserverService:
    def __init__(
        self,
        data_path: str | Path,
        source_key: SourceKey,
        control_address: tuple[str, int],
        artifact_address: tuple[str, int],
        *,
        dds_host: str = "127.0.0.1",
        timeout_seconds: float = 15.0,
        temporary_directory: str | Path | None = None,
        logger_host: str = "127.0.0.1",
        logger_port: int = 11751,
        journal_path: str | Path | None = None,
        fallback_calibration: SourceCalibrationBinding | None = None,
    ) -> None:
        self._events: queue.Queue[tuple[str, bytes]] = queue.Queue()
        self._stop = threading.Event()
        self._subsystem_uuid = observer_subsystem_uuid(source_key)
        self._logger, self._logger_transport = open_ecs_logger(
            logger_host,
            logger_port,
            origin_uuid=self._subsystem_uuid,
        )
        recorder = ObserverArtifactRecorder(
            data_path,
            source_key,
            temporary_directory=temporary_directory,
        )
        if journal_path is None:
            token = uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"{source_key.source_kind}\0{source_key.source_id}",
            ).hex
            journal_path = Path(data_path).parent / "observer-state" / f"{token}.json"
        self._coordinator = SiglentObserverCoordinator(
            source_key,
            lambda: AcquisitionClient(
                control_address,
                artifact_address,
                timeout_seconds=timeout_seconds,
            ),
            recorder.prepare_run,
            recorder.finalize_run,
            log=self._log,
            journal=ObserverRecoveryJournal(journal_path),
        )
        if fallback_calibration is not None and fallback_calibration.source_key != source_key:
            raise ValueError("Observer fallback calibration belongs to another source.")
        self._fallback_calibration = fallback_calibration
        self._resolve_run_calibration = recorder.resolve_calibration
        self._adapter = SiglentObserverDdsAdapter(
            lambda payload: self._events.put(("exposure", payload)),
            lambda payload: self._events.put(("timing", payload)),
        )
        client_uuid = uuid.uuid4()
        self._dds_client = dds_client.DDSClient(client_uuid, ip=dds_host, logger=self._logger)
        self._dds_client.when_ready().then(self._on_dds_ready)
        self._worker = threading.Thread(target=self._run, name="siglent-observer", daemon=True)
        self._worker.start()

    def _on_dds_ready(self) -> None:
        handle = self._dds_client.register_subsystem(
            f"EUV Observer [{self._coordinator.source_key.source_kind}/{self._coordinator.source_key.source_id}]",
            self._subsystem_uuid,
            temporary=True,
        )
        handle.put_status_item(
            dds_subsystem.StatusItem(
                dds_subsystem.StatusItem.STATE_INFO,
                10,
                "Configured source: "
                f"{self._coordinator.source_key.source_kind}/{self._coordinator.source_key.source_id}",
            )
        )
        self._adapter.configure(handle)
        self._log("Passive Siglent observer subscribed to exposure and laser state.", "INFO")

    def _run(self) -> None:
        self._coordinator.recover()
        while not self._stop.is_set():
            try:
                event_type, payload = self._events.get(timeout=0.1)
            except queue.Empty:
                self._coordinator.heartbeat()
                continue
            try:
                if event_type == "exposure":
                    state = decode_observer_exposure_state(
                        payload,
                        self._coordinator.source_key,
                    )
                    calibration = state.calibration
                    if state.phase == ExperimentController.RUN_STATE_PREINIT and state.run_id is not None:
                        tagged_calibration = self._resolve_run_calibration(state.run_id)
                        if tagged_calibration is not None:
                            calibration = tagged_calibration
                        elif calibration is None:
                            calibration = self._fallback_calibration
                    if (
                        state.phase == ExperimentController.RUN_STATE_PREINIT
                        and state.run_id is not None
                        and calibration is None
                    ):
                        self._log(
                            f"Skipping exposure {state.run_id}: no calibration is bound to "
                            f"{self._coordinator.source_key.source_kind}/{self._coordinator.source_key.source_id}.",
                            "WARNING",
                        )
                    self._coordinator.observe_phase(
                        state.phase,
                        run_id=state.run_id,
                        calibration=calibration,
                    )
                elif event_type == "timing":
                    self._coordinator.observe_timing(LaserTimingState.decode(payload))
            except Exception as exc:
                self._log(f"Ignored invalid observer {event_type} state: {type(exc).__name__}: {exc}", "ERROR")
            self._coordinator.heartbeat()

    def _log(self, message: str, level: str) -> None:
        print(f"[Siglent Observer] {level}: {message}", flush=True)
        if self._logger is not None:
            self._logger.log(
                message,
                level=level,
                l_type="ACQ",
                subsystem="Siglent Observer",
            )

    def ok(self) -> bool:
        return self._worker.is_alive() and self._dds_client.ok()

    def close(self) -> None:
        self._stop.set()
        self._worker.join(timeout=2.0)
        self._coordinator.close()
        self._dds_client.close()
        if self._logger_transport is not None:
            self._logger_transport.close()


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Passively attach Siglent captures to exposure records.")
    parser.add_argument("--source-id", required=True, help="Stable source ID configured on the Siglent service.")
    parser.add_argument("--source-kind", default="siglent")
    parser.add_argument("--calibration-profile-id", help="Fallback calibration profile UUID for runs without a source binding.")
    parser.add_argument("--calibration-revision", type=int, help="Fallback calibration profile revision.")
    parser.add_argument("--data-path", default=None)
    parser.add_argument("--dds-host", default="127.0.0.1")
    parser.add_argument("--capture-host", default="127.0.0.1")
    parser.add_argument("--control-port", type=int, default=11762)
    parser.add_argument("--artifact-port", type=int, default=11763)
    parser.add_argument("--timeout-seconds", type=float, default=15.0)
    parser.add_argument("--temporary-directory", default=None)
    parser.add_argument("--logger-host", default="127.0.0.1")
    parser.add_argument("--logger-port", type=int, default=11751)
    parser.add_argument("--journal-path", default=None)
    args = parser.parse_args(argv)
    if (args.calibration_profile_id is None) != (args.calibration_revision is None):
        parser.error("--calibration-profile-id and --calibration-revision must be provided together.")
    if args.calibration_revision is not None and args.calibration_revision < 1:
        parser.error("--calibration-revision must be positive.")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    data_path = args.data_path
    if data_path is None:
        root = os.environ.get("EUVL_PATH")
        if not root:
            raise RuntimeError("EUVL_PATH or --data-path is required.")
        data_path = os.path.join(root, "datasets")
    source_key = SourceKey(args.source_kind, args.source_id)
    fallback_calibration = None
    if args.calibration_profile_id is not None:
        fallback_calibration = SourceCalibrationBinding(
            source_key.source_kind,
            source_key.source_id,
            uuid.UUID(args.calibration_profile_id),
            args.calibration_revision,
        )
    service = SiglentObserverService(
        data_path,
        source_key,
        (args.capture_host, args.control_port),
        (args.capture_host, args.artifact_port),
        dds_host=args.dds_host,
        timeout_seconds=args.timeout_seconds,
        temporary_directory=args.temporary_directory,
        logger_host=args.logger_host,
        logger_port=args.logger_port,
        journal_path=args.journal_path,
        fallback_calibration=fallback_calibration,
    )
    try:
        while service.ok():
            time.sleep(0.25)
    except KeyboardInterrupt:
        pass
    finally:
        service.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())