from __future__ import annotations

import json
import os
import queue
import threading
import time
import traceback
import uuid
from dataclasses import replace
from typing import Any

from ipi_ecs.core import daemon
import ipi_ecs.core.tcp as tcp
import ipi_ecs.dds.client as client
import ipi_ecs.dds.magics as magics
import ipi_ecs.dds.types as types
from ipi_ecs.dds.subsystem import StatusItem
from ipi_ecs.logging.client import LogClient

from chamber_ctl.subsystems import uuids
from chamber_ctl.subsystems.batch_storage import BatchManifestLoadError, BatchManifestRepository
from chamber_ctl.subsystems.batcher import (
    AssessmentConfig,
    BatchAssessment,
    BatchCoordinator,
    BatchCoordinatorConfig,
    BatchDecisionKind,
    BatchHistoryStore,
    BatchManifest,
    BatchPlan,
    DdsControllerStateSource,
    ExecutionMode,
    ManifestStatus,
    batch_manifest_to_dict,
    batch_plan_from_dict,
)


BATCH_CONTROLLER_SCHEMA_VERSION = 1
BATCH_STATE_HEARTBEAT_INTERVAL = 2.0


def encode_batch_command(command: str, args: dict[str, Any] | None = None) -> bytes:
    if not isinstance(command, str) or not command.strip():
        raise ValueError("Batch command cannot be empty.")
    return json.dumps(
        {
            "schema_version": BATCH_CONTROLLER_SCHEMA_VERSION,
            "command": command,
            "args": args or {},
        },
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def decode_batch_command(payload: bytes) -> tuple[str, dict[str, Any]]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Batch command must be valid UTF-8 JSON.") from exc
    if not isinstance(value, dict) or set(value) != {"schema_version", "command", "args"}:
        raise ValueError("Batch command must contain only schema_version, command, and args.")
    if value["schema_version"] != BATCH_CONTROLLER_SCHEMA_VERSION:
        raise ValueError("Unsupported batch command schema version.")
    if not isinstance(value["command"], str) or not value["command"].strip():
        raise ValueError("Batch command cannot be empty.")
    if not isinstance(value["args"], dict):
        raise ValueError("Batch command args must be an object.")
    return value["command"].strip(), value["args"]


def _command_response(ok: bool, result: Any = None, error: str | None = None) -> bytes:
    return json.dumps(
        {
            "schema_version": BATCH_CONTROLLER_SCHEMA_VERSION,
            "ok": ok,
            "result": result,
            "error": error,
        },
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def decode_batch_state(payload: bytes) -> dict[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Batch Controller state must be valid UTF-8 JSON.") from exc
    if not isinstance(value, dict) or value.get("schema_version") != BATCH_CONTROLLER_SCHEMA_VERSION:
        raise ValueError("Unsupported Batch Controller state schema.")
    return value


def _progress_value(assessment: BatchAssessment | None) -> list[dict[str, Any]]:
    if assessment is None:
        return []
    return [
        {
            "sample": item.sample,
            "sample_number": item.sample + 1,
            "mode": item.mode.value,
            "target": item.target,
            "tolerance": item.tolerance,
            "cumulative_dose": item.cumulative_dose,
            "cumulative_runtime": item.cumulative_runtime,
            "attempt_count": item.attempt_count,
            "state": item.state.value,
            "remainder": item.remainder,
            "overshoot": item.overshoot,
        }
        for item in assessment.progress
    ]


def _assessment_value(assessment: BatchAssessment | None) -> dict[str, Any] | None:
    if assessment is None:
        return None
    decision = assessment.decision
    return {
        "decision": {
            "kind": decision.kind.value,
            "message": decision.message,
            "run_uuid": str(decision.run_uuid) if decision.run_uuid is not None else None,
            "sample": decision.sample,
            "sample_number": decision.sample + 1 if decision.sample is not None else None,
            "settings": decision.settings.get_dict() if decision.settings is not None else None,
        },
        "progress": _progress_value(assessment),
    }


def _attempt_value(attempt) -> dict[str, Any]:
    return {
        "run_uuid": str(attempt.run_uuid),
        "sample": attempt.sample,
        "sample_number": attempt.sample + 1 if attempt.sample is not None else None,
        "created_at": attempt.created_at,
        "end_time": attempt.end_time,
        "status": attempt.status,
        "end_reason": attempt.end_reason,
        "dose": attempt.dose,
        "runtime": attempt.runtime,
        "snapshot_count": attempt.snapshot_count,
        "validation_error": attempt.validation_error,
    }


class BatchControllerSubsystem:
    def __init__(self, data_path: str | None = None) -> None:
        self._run = True
        self._data_path = data_path or os.path.join(os.environ["EUVL_PATH"], "datasets")
        self._lock = threading.RLock()
        self._commands: queue.Queue[tuple[uuid.UUID, bytes, Any]] = queue.Queue()
        self._subsystem = None
        self._state_kv = None
        self._controller = None
        self._repository = None
        self._history = None
        self._coordinator: BatchCoordinator | None = None
        self._manifests: dict[uuid.UUID, BatchManifest] = {}
        self._manifest_load_errors: tuple[BatchManifestLoadError, ...] = ()
        self._active: BatchManifest | None = None
        self._display_manifest: BatchManifest | None = None
        self._assessment: BatchAssessment | None = None
        self._attempts = ()
        self._lease_owned = False
        self._execution_mode = ExecutionMode.MANUAL
        self._phase = "starting"
        self._message = "Batch Controller is starting."
        self._last_error = ""
        self._next_poll = 0.0
        self._next_state_publish = 0.0

        client_uuid = uuid.uuid4()
        self._logger_socket = tcp.TCPClientSocket()
        self._logger_socket.connect(("127.0.0.1", 11751))
        self._logger_socket.start()
        self._logger = LogClient(self._logger_socket, origin_uuid=client_uuid)

        self._did_configure = False
        self._client = client.DDSClient(client_uuid, logger=self._logger)
        self._client.when_ready().then(self._on_ready)

        self._daemon = daemon.Daemon(exception_handler=self._handle_exception)
        self._daemon.add(self._worker)
        self._daemon.start()

    def _handle_exception(self, exc: Exception) -> None:
        self._last_error = f"{type(exc).__name__}: {exc}"
        self._phase = "error"
        for line in traceback.format_exception(None, exc, exc.__traceback__):
            for split in line.splitlines():
                if split:
                    self._logger.log(split, level="ERROR", l_type="CTRL", subsystem="Exposure Batch Controller")

    def _on_ready(self) -> None:
        with self._lock:
            if self._did_configure:
                return
            self._did_configure = True
            handle = self._client.register_subsystem(
                "Exposure Batch Controller",
                uuids.UUID_EXPOSURE_BATCH_CONTROLLER,
            )
            self._subsystem = handle
            self._state_kv = handle.get_kv_property(b"state", False, True, True)
            self._state_kv.set_type(types.ByteTypeSpecifier())
            handle.add_event_handler(b"batch_command").on_called(self._on_command)
            self._controller = DdsControllerStateSource(
                registered_handle=handle,
                on_start_progress=self._on_start_progress,
            )
            self._publish_state()

    def _on_start_progress(self, message: str) -> None:
        self._phase = "starting"
        self._message = message
        self._publish_state()

    def _on_command(self, sender_uuid, payload, handle) -> None:
        self._commands.put((sender_uuid, payload, handle))
        handle.feedback(magics.OP_IN_PROGRESS + b": Batch command queued.")

    def _load_manifests(self) -> None:
        # A restarted controller always requires a deliberate decision before it resumes automation.
        self._execution_mode = ExecutionMode.MANUAL
        manifests = self._repository.list()
        self._manifest_load_errors = self._repository.load_errors
        self._manifests = {manifest.batch_uuid: manifest for manifest in manifests}
        unreadable_active = tuple(
            error
            for error in self._manifest_load_errors
            if error.status == ManifestStatus.ACTIVE.value
        )
        if unreadable_active:
            self._phase = "error"
            self._last_error = unreadable_active[0].error
            self._message = (
                f"Cannot resume batch automation: {len(unreadable_active)} active batch manifest(s) could not be read."
            )
            return
        if self._manifest_load_errors:
            self._last_error = self._manifest_load_errors[0].error
        active = [manifest for manifest in manifests if manifest.status is ManifestStatus.ACTIVE]
        if len(active) > 1:
            raise RuntimeError("More than one active batch manifest was restored.")
        if active:
            restored = replace(active[0], paused=True, updated_at=time.time())
            self._repository.save_control_state(restored)
            self._manifests[restored.batch_uuid] = restored
            self._active = restored
            self._display_manifest = restored
            self._phase = "restart_paused"
            self._message = "Active batch restored paused; operator Resume is required."
        else:
            terminal = [
                manifest
                for manifest in manifests
                if manifest.status in (ManifestStatus.COMPLETED, ManifestStatus.CANCELLED)
            ]
            self._display_manifest = max(
                terminal,
                key=lambda manifest: (manifest.updated_at, str(manifest.batch_uuid)),
                default=None,
            )
            self._phase = "idle"
            self._message = (
                "No active batch."
                if not self._manifest_load_errors
                else f"No active batch; skipped {len(self._manifest_load_errors)} unreadable manifest(s)."
            )

    def _has_unreadable_active_manifest(self) -> bool:
        return any(error.status == ManifestStatus.ACTIVE.value for error in self._manifest_load_errors)

    def _ensure_active_runtime(self) -> None:
        if self._active is None or self._controller is None or self._coordinator is not None:
            return
        if not self._lease_owned:
            self._controller.acquire_automation("Exposure Batch Controller")
            self._lease_owned = True
        self._coordinator = BatchCoordinator(
            self._active.plan,
            self._active.batch_uuid,
            self._history,
            self._controller,
            config=BatchCoordinatorConfig(assessment=AssessmentConfig()),
            output=self._log_output,
            acknowledged_runs=self._active.acknowledged_runs,
            manually_paused=self._active.paused,
            owns_sources=False,
        )
        self._next_poll = 0.0

    def _log_output(self, message: str) -> None:
        self._logger.log(message, level="INFO", l_type="CTRL", subsystem="Exposure Batch Controller")

    def _sync_active_control(self) -> None:
        if self._active is None or self._coordinator is None:
            return
        acknowledged = tuple(sorted(self._coordinator.acknowledged_runs, key=str))
        if acknowledged != self._active.acknowledged_runs:
            self._active = replace(
                self._active,
                acknowledged_runs=acknowledged,
                updated_at=time.time(),
            )
            self._repository.save_control_state(self._active)
            self._manifests[self._active.batch_uuid] = self._active

    def _poll_active(self, *, allow_automatic_start: bool = True) -> None:
        if self._active is None:
            return
        self._ensure_active_runtime()
        if self._coordinator is None:
            return
        self._assessment = self._coordinator.poll_once()
        self._attempts = self._history.load_attempts(self._active.batch_uuid)
        self._sync_active_control()
        decision = self._assessment.decision

        if self._active.cancel_pending:
            controller_state = self._controller.snapshot()
            if controller_state.available and controller_state.idle and decision.kind not in (
                BatchDecisionKind.WAIT_ACTIVE,
                BatchDecisionKind.WAIT_FINALIZATION,
                BatchDecisionKind.REPAIR_METRICS,
            ):
                self._finish_active(ManifestStatus.CANCELLED, "Batch cancelled after current exposure.")
            else:
                self._phase = "cancelling"
                self._message = "Cancellation pending; waiting for current exposure data."
            return

        if decision.kind is BatchDecisionKind.COMPLETE:
            self._finish_active(ManifestStatus.COMPLETED, decision.message)
            return
        if decision.kind in (BatchDecisionKind.PAUSE_ERROR, BatchDecisionKind.PAUSE_FAILURE):
            self._phase = "error_paused" if decision.kind is BatchDecisionKind.PAUSE_ERROR else "failure_paused"
            self._message = decision.message
            return
        if self._active.paused:
            self._phase = "paused"
            self._message = decision.message if decision.kind is BatchDecisionKind.PAUSED else "Batch is paused."
            return
        if decision.kind is BatchDecisionKind.START_REMAINDER:
            if self._execution_mode is ExecutionMode.MANUAL:
                self._phase = "waiting_continue"
                self._message = "Manual mode is ready; operator Continue is required."
                return
            if allow_automatic_start:
                ok, message = self._coordinator.start_next(
                    run_tags={"batch_revision": self._active.revision}
                )
                self._phase = "starting" if ok else "start_error"
                self._message = message
                return
        self._phase = decision.kind.value
        self._message = decision.message

    def _finish_active(self, status: ManifestStatus, message: str) -> None:
        if self._active is None:
            return
        finished = replace(
            self._active,
            status=status,
            paused=True,
            cancel_pending=False,
            acknowledged_runs=tuple(sorted(self._coordinator.acknowledged_runs, key=str)) if self._coordinator else (),
            updated_at=time.time(),
        )
        self._repository.save_control_state(finished)
        self._manifests[finished.batch_uuid] = finished
        if self._coordinator is not None:
            self._coordinator.close()
        self._coordinator = None
        if self._lease_owned and self._controller is not None:
            self._controller.release_automation()
            self._lease_owned = False
        self._active = None
        self._display_manifest = finished
        self._phase = status.value
        self._message = message

    def _manifest(self, raw_uuid) -> BatchManifest:
        batch_uuid = uuid.UUID(str(raw_uuid))
        manifest = self._manifests.get(batch_uuid)
        if manifest is None:
            raise ValueError(f"Batch manifest {batch_uuid} was not found.")
        return manifest

    @staticmethod
    def _mode(value) -> ExecutionMode:
        return ExecutionMode(str(value))

    def _execute_command(self, sender_uuid: uuid.UUID, command: str, args: dict[str, Any]) -> Any:
        now = time.time()
        if command == "create":
            plan = batch_plan_from_dict(args["plan"])
            status = ManifestStatus(args.get("status", ManifestStatus.DRAFT.value))
            if status not in (ManifestStatus.DRAFT, ManifestStatus.SUBMITTED):
                raise ValueError("New manifests must be draft or submitted.")
            manifest = BatchManifest(
                batch_uuid=uuid.uuid4(),
                revision=1,
                status=status,
                mode=self._mode(args.get("mode", ExecutionMode.MANUAL.value)),
                plan=plan,
                created_at=now,
                updated_at=now,
                origin=str(args.get("origin", "local")),
                submitted_by=str(args.get("submitted_by", "")),
                revision_note=str(args.get("revision_note", "Created")),
            )
            self._repository.create(manifest)
            self._manifests[manifest.batch_uuid] = manifest
            return batch_manifest_to_dict(manifest)

        if command == "get_manifest":
            return batch_manifest_to_dict(self._manifest(args["batch_uuid"]))

        if command == "update":
            current = self._manifest(args["batch_uuid"])
            if current.status in (ManifestStatus.COMPLETED, ManifestStatus.CANCELLED, ManifestStatus.WITHDRAWN):
                raise ValueError("Archived manifests cannot be edited.")
            if current.status is ManifestStatus.ACTIVE:
                controller_state = self._controller.snapshot()
                if not controller_state.available or not controller_state.idle:
                    raise ValueError("Active batch plans cannot be edited while an exposure is running.")
            expected_revision = int(args["expected_revision"])
            updated = replace(
                current,
                revision=expected_revision + 1,
                plan=batch_plan_from_dict(args["plan"]),
                mode=self._mode(args.get("mode", current.mode.value)),
                updated_at=now,
                revision_note=str(args.get("revision_note", "Updated")),
            )
            self._repository.save_revision(updated, expected_revision=expected_revision)
            self._manifests[updated.batch_uuid] = updated
            if self._active is not None and self._active.batch_uuid == updated.batch_uuid:
                self._active = updated
                self._coordinator.update_plan(updated.plan)
            return batch_manifest_to_dict(updated)

        if command == "activate":
            if self._has_unreadable_active_manifest():
                raise ValueError("Cannot activate a batch while an unreadable active manifest requires repair.")
            if self._active is not None:
                raise ValueError(f"Batch {self._active.batch_uuid} is already active.")
            manifest = self._manifest(args["batch_uuid"])
            if manifest.status not in (ManifestStatus.DRAFT, ManifestStatus.SUBMITTED, ManifestStatus.CANCELLED):
                raise ValueError("Only draft, submitted, or cancelled manifests can be activated.")
            if not manifest.plan.entries:
                raise ValueError("Cannot activate an empty batch plan.")
            self._controller.acquire_automation("Exposure Batch Controller")
            self._lease_owned = True
            active = replace(
                manifest,
                status=ManifestStatus.ACTIVE,
                paused=False,
                cancel_pending=False,
                updated_at=now,
            )
            self._repository.save_control_state(active)
            self._manifests[active.batch_uuid] = active
            self._active = active
            self._display_manifest = active
            self._ensure_active_runtime()
            self._poll_active(allow_automatic_start=True)
            return batch_manifest_to_dict(active)

        if command == "pause":
            if self._active is None:
                raise ValueError("No active batch.")
            self._coordinator.pause()
            self._active = replace(self._active, paused=True, updated_at=now)
            self._repository.save_control_state(self._active)
            self._manifests[self._active.batch_uuid] = self._active
            self._poll_active(allow_automatic_start=False)
            return {"phase": self._phase}

        if command == "resume":
            if self._active is None:
                raise ValueError("No active batch.")
            self._coordinator.resume()
            self._active = replace(self._active, paused=False, updated_at=now)
            self._repository.save_control_state(self._active)
            self._manifests[self._active.batch_uuid] = self._active
            self._poll_active(allow_automatic_start=True)
            return {"phase": self._phase}

        if command == "continue":
            if self._active is None or self._execution_mode is not ExecutionMode.MANUAL:
                raise ValueError("Continue is available only for an active manual batch.")
            if self._active.paused:
                raise ValueError("Resume the batch before continuing.")
            self._poll_active(allow_automatic_start=False)
            if self._assessment is None or self._assessment.decision.kind is not BatchDecisionKind.START_REMAINDER:
                raise ValueError("Manual Continue is accepted only while waiting for the next exposure.")
            ok, message = self._coordinator.start_next(
                run_tags={"batch_revision": self._active.revision}
            )
            if not ok:
                raise RuntimeError(message)
            self._phase = "starting"
            self._message = message
            return {"message": message}

        if command == "set_mode":
            self._execution_mode = self._mode(args["mode"])
            if self._active is not None:
                self._poll_active(allow_automatic_start=True)
            return {"mode": self._execution_mode.value}

        if command == "cancel":
            if self._active is None:
                raise ValueError("No active batch.")
            self._coordinator.pause()
            self._active = replace(
                self._active,
                paused=True,
                cancel_pending=True,
                updated_at=now,
            )
            self._repository.save_control_state(self._active)
            self._manifests[self._active.batch_uuid] = self._active
            self._poll_active(allow_automatic_start=False)
            return {"phase": self._phase}

        if command == "withdraw":
            manifest = self._manifest(args["batch_uuid"])
            if manifest.status is ManifestStatus.ACTIVE:
                raise ValueError("Use Cancel for the active batch.")
            if manifest.status in (ManifestStatus.COMPLETED, ManifestStatus.CANCELLED, ManifestStatus.WITHDRAWN):
                raise ValueError("Manifest is already archived.")
            withdrawn = replace(manifest, status=ManifestStatus.WITHDRAWN, paused=True, updated_at=now)
            self._repository.save_control_state(withdrawn)
            self._manifests[withdrawn.batch_uuid] = withdrawn
            return batch_manifest_to_dict(withdrawn)

        if command == "unwithdraw":
            manifest = self._manifest(args["batch_uuid"])
            if manifest.status is not ManifestStatus.WITHDRAWN:
                raise ValueError("Only withdrawn manifests can be restored.")
            restored = replace(
                manifest,
                status=ManifestStatus.DRAFT,
                paused=True,
                cancel_pending=False,
                updated_at=now,
                revision_note="Restored from withdrawn",
            )
            self._repository.save_control_state(restored)
            self._manifests[restored.batch_uuid] = restored
            return batch_manifest_to_dict(restored)

        if command == "refresh":
            self._poll_active(allow_automatic_start=False)
            return {"phase": self._phase}

        raise ValueError(f"Unknown batch command {command!r}.")

    def _handle_command(self, sender_uuid, payload, handle) -> None:
        try:
            command, args = decode_batch_command(payload)
            result = self._execute_command(sender_uuid, command, args)
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            handle.fail(_command_response(False, error=self._last_error))
        else:
            self._last_error = ""
            handle.ret(_command_response(True, result=result))
        finally:
            self._publish_state()

    def _phase_for_state(self) -> str:
        return self._phase

    def _state_value(self) -> dict[str, Any]:
        manifests = sorted(self._manifests.values(), key=lambda item: (item.updated_at, str(item.batch_uuid)), reverse=True)
        display_manifest = self._active or self._display_manifest
        return {
            "schema_version": BATCH_CONTROLLER_SCHEMA_VERSION,
            "emitted_at": time.time(),
            "phase": self._phase_for_state(),
            "message": self._message,
            "last_error": self._last_error or None,
            "lease_owned": self._lease_owned,
            "execution_mode": self._execution_mode.value,
            "active_batch_uuid": str(self._active.batch_uuid) if self._active is not None else None,
            "active_manifest": batch_manifest_to_dict(self._active) if self._active is not None else None,
            "display_manifest": batch_manifest_to_dict(display_manifest) if display_manifest is not None else None,
            "assessment": _assessment_value(self._assessment),
            "attempts": [_attempt_value(attempt) for attempt in self._attempts],
            "manifest_load_errors": [
                {
                    "entry_uuid": str(error.entry_uuid),
                    "batch_uuid": error.batch_uuid,
                    "status": error.status,
                    "error": error.error,
                }
                for error in self._manifest_load_errors
            ],
            "manifests": [
                {
                    "batch_uuid": str(manifest.batch_uuid),
                    "revision": manifest.revision,
                    "status": manifest.status.value,
                    "mode": manifest.mode.value,
                    "name": manifest.plan.template.name,
                    "description": manifest.plan.template.description,
                    "entry_count": len(manifest.plan.entries),
                    "updated_at": manifest.updated_at,
                    "origin": manifest.origin,
                    "submitted_by": manifest.submitted_by,
                }
                for manifest in manifests
            ],
        }

    def _publish_status_items(self) -> None:
        if self._subsystem is None:
            return
        primary = StatusItem(StatusItem.STATE_INFO, 0, self._phase.replace("_", " ").title())
        self._subsystem.put_status_item(primary)
        if self._last_error:
            self._subsystem.put_status_item(StatusItem(StatusItem.STATE_ALARM, 200, self._last_error))
        elif self._subsystem.get_status_item_exists(200):
            self._subsystem.clear_status_item(200)
        if self._phase in ("failure_paused", "restart_paused", "paused"):
            self._subsystem.put_status_item(StatusItem(StatusItem.STATE_WARN, 100, self._message))
        elif self._subsystem.get_status_item_exists(100):
            self._subsystem.clear_status_item(100)

    def _publish_state(self) -> None:
        with self._lock:
            if self._state_kv is not None:
                self._state_kv.value = json.dumps(
                    self._state_value(),
                    allow_nan=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            self._publish_status_items()

    def _worker(self, stop_flag: daemon.StopFlag) -> None:
        self._repository = BatchManifestRepository(self._data_path)
        self._history = BatchHistoryStore(self._data_path)
        try:
            self._load_manifests()
            while stop_flag.run() and self._run:
                try:
                    sender_uuid, payload, handle = self._commands.get(timeout=0.25)
                except queue.Empty:
                    pass
                else:
                    self._handle_command(sender_uuid, payload, handle)

                if self._active is not None and self._controller is not None:
                    try:
                        self._ensure_active_runtime()
                        if time.monotonic() >= self._next_poll:
                            self._poll_active(allow_automatic_start=True)
                            self._next_poll = time.monotonic() + 10.0
                    except Exception as exc:
                        self._last_error = f"{type(exc).__name__}: {exc}"
                        self._phase = "error"
                        self._message = self._last_error
                if time.monotonic() >= self._next_state_publish:
                    self._publish_state()
                    self._next_state_publish = time.monotonic() + BATCH_STATE_HEARTBEAT_INTERVAL
        finally:
            if self._coordinator is not None:
                self._coordinator.close()
            if self._lease_owned and self._controller is not None:
                try:
                    self._controller.release_automation()
                except Exception:
                    pass
            if self._controller is not None:
                self._controller.close()
            self._history.close()
            self._repository.close()

    def ok(self) -> bool:
        return self._run and self._client.ok() and self._daemon.is_ok()

    def close(self) -> None:
        self._run = False
        self._daemon.stop()
        self._client.close()
        self._logger_socket.close()


def main(stop_event=None):
    subsystem = BatchControllerSubsystem()
    try:
        while subsystem.ok() and not (stop_event is not None and stop_event.is_set()):
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        subsystem.close()


if __name__ == "__main__":
    main(None)