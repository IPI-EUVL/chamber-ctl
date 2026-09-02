from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from ipi_ecs.subsystems.experiment_controller import ExperimentReader

from chamber_ctl.data.acquisition_artifacts import AcquisitionArtifactImporter
from chamber_ctl.data.analysis_selection import (
    ACTIVE_DOSE_PRODUCT_TAG,
    ActiveDoseProduct,
    encode_active_dose_product_tag,
)
from chamber_ctl.data.calibration import (
    PRIMARY_SOURCE_TAG,
    SOURCE_CALIBRATIONS_TAG,
    CalibrationProfile,
    CalibrationRepository,
    SourceCalibrationBinding,
    SourceKey,
    source_calibration_for_source,
    source_configuration_from_run_tags,
)
from chamber_ctl.data.exposure_graph import ensure_exposure_graph
from chamber_ctl.data.observer_analysis import (
    CAPTURED_ALGORITHM,
    observer_analysis_filename,
    write_observer_dose_products,
)
from euv_acquisition.models import SnapshotCloseReason
from euv_acquisition.snapshot import SnapshotContents, read_snapshot
from euv_acquisition.sources.siglent import (
    SIGLENT_BATCH_KIND,
    SIGLENT_NATIVE_ANALYSIS_VERSION,
)


OBSERVER_CAPTURE_SCHEMA_VERSION = 1
OBSERVER_CAPTURE_RESOURCE_TYPE = "euv_observer_capture"
OBSERVER_CONTEXT_SCHEMA_VERSION = 1
OBSERVER_CONTEXT_RESOURCE_TYPE = "euv_observer_context"


def observer_capture_filename(session_id: uuid.UUID) -> str:
    return f"euv_observer_capture_{session_id}.json"


def observer_context_filename(session_id: uuid.UUID) -> str:
    return f"euv_observer_context_{session_id}.json"


class ObserverArtifactRecorder:
    def __init__(
        self,
        data_path: str | Path,
        source_key: SourceKey,
        *,
        temporary_directory: str | Path | None = None,
        expected_native_analysis_version: str = SIGLENT_NATIVE_ANALYSIS_VERSION,
        expected_batch_kind: str = SIGLENT_BATCH_KIND,
    ) -> None:
        self.data_path = Path(data_path)
        self.source_key = source_key
        self.temporary_directory = None if temporary_directory is None else Path(temporary_directory)
        self.expected_native_analysis_version = expected_native_analysis_version
        self.expected_batch_kind = expected_batch_kind

    def resolve_calibration(self, run_id: uuid.UUID) -> SourceCalibrationBinding | None:
        reader = ExperimentReader(str(self.data_path), "exposure")
        try:
            tags = reader.get_run(run_id).get_record().get_tags()
        finally:
            reader.close()
        value = tags.get(SOURCE_CALIBRATIONS_TAG)
        if value is None:
            return None
        return source_calibration_for_source(value, self.source_key)

    def load_calibration(self, binding: SourceCalibrationBinding) -> CalibrationProfile:
        if binding.source_key != self.source_key:
            raise ValueError("Observer calibration belongs to another source.")
        repository = CalibrationRepository(self.data_path)
        try:
            profile = repository.get(binding.profile_id, binding.revision)
        finally:
            repository.close()
        if profile is None:
            raise ValueError(
                f"Observer calibration {binding.profile_id} revision "
                f"{binding.revision} was not found."
            )
        return profile

    def prepare_run(self, run, _client) -> None:
        profile = self._load_calibration(run)
        reader = ExperimentReader(str(self.data_path), "exposure")
        try:
            entry = reader.get_run(run.run_id).get_record()
            descriptor = self._descriptor(run, profile, status="capturing", snapshot_ids=[])
            descriptor_name = observer_capture_filename(run.session_id)
            if descriptor_name in dict(entry.list_resources()):
                self._require_descriptor_identity(entry, run, profile)
            else:
                self._write_json_resource(
                    entry,
                    descriptor_name,
                    OBSERVER_CAPTURE_RESOURCE_TYPE,
                    descriptor,
                )
            self._write_or_require_context(entry, run)
        finally:
            reader.close()

    def finalize_run(self, run, session, client) -> None:
        self._validate_session(run, session)
        profile = self._load_calibration(run)
        reader = ExperimentReader(str(self.data_path), "exposure")
        try:
            entry = reader.get_run(run.run_id).get_record()
            self._require_descriptor(entry, run, profile, session)
            importer = AcquisitionArtifactImporter(client, self.temporary_directory)
            for stored in session.snapshots:
                manifest = stored.manifest
                if stored.acknowledged:
                    self._require_persisted_snapshot(entry, run, manifest.snapshot_id, manifest.filename)
                    continue
                validated: dict[str, SnapshotContents] = {}

                def validate(_manifest, local_path: Path) -> None:
                    contents = read_snapshot(local_path)
                    self._validate_snapshot(run, manifest, contents)
                    validated["contents"] = contents

                def persist_context(persisted_entry) -> None:
                    contents = validated.get("contents")
                    if contents is None:
                        raise RuntimeError("Observer snapshot validation did not produce context.")
                    self._append_snapshot_context(persisted_entry, run, contents)

                importer.import_snapshot(
                    entry,
                    manifest,
                    validate=validate,
                    before_ack=persist_context,
                )

            snapshot_ids = [stored.manifest.snapshot_id for stored in session.snapshots]
            context = self._read_json_resource(
                entry,
                observer_context_filename(run.session_id),
                OBSERVER_CONTEXT_RESOURCE_TYPE,
            )
            products = write_observer_dose_products(
                entry,
                self.data_path,
                run,
                profile,
                snapshot_ids,
                context,
                expected_native_analysis_version=self.expected_native_analysis_version,
            )
            configuration = (
                source_configuration_from_run_tags(entry.get_tags())
                if PRIMARY_SOURCE_TAG in entry.get_tags()
                else None
            )
            if configuration is not None and configuration.primary_source == self.source_key:
                captured = next(
                    product
                    for product in products
                    if product.analysis.algorithm == CAPTURED_ALGORITHM
                )
                analysis = captured.analysis
                entry.set_tag(
                    ACTIVE_DOSE_PRODUCT_TAG,
                    encode_active_dose_product_tag(
                        ActiveDoseProduct(
                            self.source_key,
                            analysis.algorithm,
                            observer_analysis_filename(analysis.session_id, analysis.algorithm),
                        )
                    ),
                )
                entry.set_tag("dose", analysis.total_dose_mj_cm2)
                entry.set_tag("calibration_profile_id", str(analysis.calibration.profile_id))
                entry.set_tag("calibration_revision", str(analysis.calibration.revision))
                entry.set_tag("calibration_hash", analysis.calibration.content_hash)
                entry.remove_tag("active_dose_analysis")
                graph_result = ensure_exposure_graph(
                    run.run_id,
                    entry,
                    self.data_path,
                    allow_incomplete=True,
                )
                if graph_result.graph is None:
                    raise RuntimeError("Primary observer dose graph is not ready after product finalization.")
            descriptor = self._descriptor(
                run,
                profile,
                status="complete",
                snapshot_ids=[str(snapshot_id) for snapshot_id in snapshot_ids],
            )
            self._write_json_resource(
                entry,
                observer_capture_filename(run.session_id),
                OBSERVER_CAPTURE_RESOURCE_TYPE,
                descriptor,
            )
        finally:
            reader.close()

    def _load_calibration(self, run) -> CalibrationProfile:
        return self.load_calibration(run.calibration)

    def _descriptor(
        self,
        run,
        profile: CalibrationProfile,
        *,
        status: str,
        snapshot_ids: list[str],
    ) -> dict:
        return {
            "schema_version": OBSERVER_CAPTURE_SCHEMA_VERSION,
            "role": "observer",
            "run_id": str(run.run_id),
            "session_id": str(run.session_id),
            "source_kind": self.source_key.source_kind,
            "source_id": self.source_key.source_id,
            "status": status,
            "capture_started_unix_ns": run.capture_started_unix_ns,
            "stopped_observed_unix_ns": run.stopped_observed_unix_ns,
            "native_analysis_version": self.expected_native_analysis_version,
            "calibration": profile.to_dict(),
            "snapshot_ids": snapshot_ids,
            "timing_observation_count": len(run.timing_observations),
        }

    def _empty_context(self, run) -> dict:
        return {
            "schema_version": OBSERVER_CONTEXT_SCHEMA_VERSION,
            "run_id": str(run.run_id),
            "session_id": str(run.session_id),
            "source_kind": self.source_key.source_kind,
            "source_id": self.source_key.source_id,
            "snapshots": [],
        }

    def _validate_session(self, run, session) -> None:
        if session.session_id != run.session_id:
            raise ValueError("Observer finalization received the wrong capture session.")
        if (session.source_kind, session.source_id) != (
            self.source_key.source_kind,
            self.source_key.source_id,
        ):
            raise ValueError("Observer finalization received a session from another source.")

    def _validate_snapshot(self, run, manifest, contents: SnapshotContents) -> None:
        if manifest.session_id != run.session_id or contents.session_id != run.session_id:
            raise ValueError("Observer snapshot belongs to another capture session.")
        if (contents.source_kind, contents.source_id) != (
            self.source_key.source_kind,
            self.source_key.source_id,
        ):
            raise ValueError("Observer snapshot belongs to another source.")
        if contents.close_reason is not SnapshotCloseReason.SOURCE_BATCH:
            raise ValueError("Observer snapshot is not an atomic source batch.")
        if contents.source_batch is None or contents.source_batch.batch_kind != self.expected_batch_kind:
            raise ValueError("Observer snapshot has the wrong source batch kind.")
        if contents.native_analysis_version != self.expected_native_analysis_version:
            raise ValueError("Observer snapshot has the wrong native analysis version.")

    def _append_snapshot_context(self, entry, run, contents: SnapshotContents) -> None:
        filename = observer_context_filename(run.session_id)
        context = self._read_json_resource(entry, filename, OBSERVER_CONTEXT_RESOURCE_TYPE)
        expected_identity = self._empty_context(run)
        for key in ("schema_version", "run_id", "session_id", "source_kind", "source_id"):
            if context.get(key) != expected_identity[key]:
                raise ValueError("Observer context identity does not match the active capture.")
        if not isinstance(context.get("snapshots"), list):
            raise ValueError("Observer context snapshots must be a list.")
        source_batch = contents.source_batch
        if source_batch is None:
            raise ValueError("Observer source batch context is missing.")
        exposure_start = (
            {"state": "null"}
            if run.exposure_started_unix_ns is None
            else {"state": "value", "value": run.exposure_started_unix_ns}
        )
        item = {
            "snapshot_id": str(contents.snapshot_id),
            "capture_batch_id": str(source_batch.batch_id),
            "capture_batch_kind": source_batch.batch_kind,
            "capture_started_unix_ns": source_batch.capture_started_unix_ns,
            "capture_completed_unix_ns": source_batch.capture_completed_unix_ns,
            "is_step_exposure": {"state": "unknown"},
            "exposure_start_ns": exposure_start,
            "laser_off_eligibility": self._laser_eligibility(run, source_batch),
        }
        matches = [value for value in context["snapshots"] if value.get("snapshot_id") == item["snapshot_id"]]
        if matches and matches != [item]:
            raise ValueError("Observer snapshot context conflicts with an existing entry.")
        if not matches:
            context["snapshots"].append(item)
        self._write_json_resource(entry, filename, OBSERVER_CONTEXT_RESOURCE_TYPE, context)

    @staticmethod
    def _laser_eligibility(run, source_batch) -> dict:
        observations = [
            observation
            for observation in run.timing_observations
            if source_batch.capture_started_unix_ns
            <= observation.sampled_at_unix_ns
            <= source_batch.capture_completed_unix_ns
        ]
        if not observations:
            return {"state": "unknown", "evidence_count": 0, "clock_bases": []}
        if (
            run.timing_evidence_started_unix_ns is not None
            and source_batch.capture_started_unix_ns < run.timing_evidence_started_unix_ns
            and all(observation.transmitting for observation in observations)
        ):
            return {
                "state": "unknown",
                "evidence_count": len(observations),
                "clock_bases": sorted({observation.clock_basis for observation in observations}),
                "reason": "incomplete_timing_history",
            }
        state = "eligible" if all(observation.transmitting for observation in observations) else "ineligible"
        return {
            "state": state,
            "evidence_count": len(observations),
            "clock_bases": sorted({observation.clock_basis for observation in observations}),
        }

    def _require_descriptor(self, entry, run, profile: CalibrationProfile, session) -> None:
        value = self._require_descriptor_identity(entry, run, profile)
        status = value.get("status")
        snapshot_ids = value.get("snapshot_ids")
        expected_snapshot_ids = [str(stored.manifest.snapshot_id) for stored in session.snapshots]
        if status == "capturing" and snapshot_ids != []:
            raise ValueError("Capturing observer descriptor already lists finalized snapshots.")
        if status == "complete" and snapshot_ids != expected_snapshot_ids:
            raise ValueError("Complete observer descriptor lists different snapshots.")
        if status not in {"capturing", "complete"}:
            raise ValueError("Observer capture descriptor has an invalid status.")

    def _require_descriptor_identity(self, entry, run, profile: CalibrationProfile) -> dict:
        value = self._read_json_resource(
            entry,
            observer_capture_filename(run.session_id),
            OBSERVER_CAPTURE_RESOURCE_TYPE,
        )
        expected = self._descriptor(run, profile, status="capturing", snapshot_ids=[])
        for mutable in (
            "status",
            "snapshot_ids",
            "stopped_observed_unix_ns",
            "timing_observation_count",
        ):
            expected[mutable] = value.get(mutable)
        if value != expected:
            raise ValueError("Observer capture descriptor does not match the session being finalized.")
        return value

    def _require_persisted_snapshot(self, entry, run, snapshot_id: uuid.UUID, filename: str) -> None:
        resources = dict(entry.list_resources())
        if filename not in resources:
            raise ValueError(f"Acknowledged observer snapshot {snapshot_id} is absent from the run record.")
        context = self._read_json_resource(
            entry,
            observer_context_filename(run.session_id),
            OBSERVER_CONTEXT_RESOURCE_TYPE,
        )
        if not any(item.get("snapshot_id") == str(snapshot_id) for item in context.get("snapshots", [])):
            raise ValueError(f"Acknowledged observer snapshot {snapshot_id} has no persisted context.")

    def _write_or_require_context(self, entry, run) -> None:
        filename = observer_context_filename(run.session_id)
        resources = dict(entry.list_resources())
        if filename in resources:
            value = self._read_json_resource(entry, filename, OBSERVER_CONTEXT_RESOURCE_TYPE)
            snapshots = value.get("snapshots")
            normalized = dict(value)
            normalized["snapshots"] = []
            if not isinstance(snapshots, list) or normalized != self._empty_context(run):
                raise ValueError(f"Existing observer resource {filename} conflicts with this capture.")
            return
        self._write_json_resource(
            entry,
            filename,
            OBSERVER_CONTEXT_RESOURCE_TYPE,
            self._empty_context(run),
        )

    @staticmethod
    def _read_json_resource(entry, filename: str, resource_type: str) -> dict:
        resources = dict(entry.list_resources())
        if resources.get(filename) != resource_type:
            raise ValueError(f"Observer resource {filename} is missing or has the wrong type.")
        with entry.resource(filename, resource_type, "r") as resource:
            value = json.load(resource)
        if not isinstance(value, dict):
            raise ValueError(f"Observer resource {filename} must contain a JSON object.")
        return value

    def _write_json_resource(self, entry, filename: str, resource_type: str, value: dict) -> None:
        directory = self.data_path / entry.get_foldername()
        destination = directory / filename
        temporary = directory / f".{filename}.{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as output:
                json.dump(value, output, allow_nan=False, sort_keys=True, separators=(",", ":"))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, destination)
            entry.register_existing_resource(filename, resource_type)
        finally:
            temporary.unlink(missing_ok=True)