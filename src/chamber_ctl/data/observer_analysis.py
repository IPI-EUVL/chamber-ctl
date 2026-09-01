from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import h5py
import numpy as np

from chamber_ctl.data.calibration import CalibrationProfile, SourceKey
from chamber_ctl.data.dose_analysis import legacy_default_profile
from chamber_ctl.data.legacy_siglent import (
    EXPOSURE_START_UNSPECIFIED,
    LEGACY_ANALYSIS_VERSION,
    analyze_legacy_siglent_values,
    legacy_siglent_dose_for_integral,
)
from euv_acquisition.snapshot import SnapshotContents, read_snapshot


OBSERVER_ANALYSIS_SCHEMA_VERSION = 1
OBSERVER_ANALYSIS_RESOURCE_TYPE = "euv_observer_dose_analysis"
OBSERVER_GRAPH_SCHEMA_VERSION = 1
OBSERVER_GRAPH_RESOURCE_TYPE = "euv_observer_dose_graph"
CAPTURED_ALGORITHM = "captured"
LEGACY_COMPENSATED_ALGORITHM = "legacy_compensated"
CAPTURED_ANALYSIS_VERSION = "siglent-captured-v1-native-integral-sum"
OBSERVER_GRAPH_ALGORITHM_VERSION = "observer-dose-graph-v1"
FULL_POINT_LIMIT = 10_000
THUMBNAIL_POINT_LIMIT = 1_000

ObserverAlgorithm = Literal["captured", "legacy_compensated"]


def observer_analysis_filename(session_id: uuid.UUID, algorithm: ObserverAlgorithm) -> str:
    _require_algorithm(algorithm)
    return f"euv_observer_dose_analysis_{session_id}_{algorithm}.json"


def observer_graph_filename(session_id: uuid.UUID, algorithm: ObserverAlgorithm) -> str:
    _require_algorithm(algorithm)
    return f"euv_observer_dose_graph_{session_id}_{algorithm}.h5"


@dataclass(frozen=True)
class ObserverCompleteness:
    snapshot_count: int
    included_snapshot_count: int
    excluded_snapshot_count: int
    unknown_eligibility_snapshot_count: int
    unknown_step_mode_snapshot_count: int

    def to_dict(self) -> dict:
        return {
            "snapshot_count": self.snapshot_count,
            "included_snapshot_count": self.included_snapshot_count,
            "excluded_snapshot_count": self.excluded_snapshot_count,
            "unknown_eligibility_snapshot_count": self.unknown_eligibility_snapshot_count,
            "unknown_step_mode_snapshot_count": self.unknown_step_mode_snapshot_count,
        }

    @classmethod
    def from_dict(cls, value: object) -> "ObserverCompleteness":
        expected = {
            "snapshot_count",
            "included_snapshot_count",
            "excluded_snapshot_count",
            "unknown_eligibility_snapshot_count",
            "unknown_step_mode_snapshot_count",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("Observer analysis completeness contains unknown or missing fields.")
        counts = {}
        for name in expected:
            item = value[name]
            if isinstance(item, bool) or not isinstance(item, int) or item < 0:
                raise ValueError(f"Observer analysis completeness {name} must be a non-negative integer.")
            counts[name] = item
        if counts["included_snapshot_count"] + counts["excluded_snapshot_count"] != counts["snapshot_count"]:
            raise ValueError("Observer analysis included and excluded snapshot counts are inconsistent.")
        return cls(**counts)


@dataclass(frozen=True)
class ObserverDoseAnalysis:
    run_id: uuid.UUID
    session_id: uuid.UUID
    source_key: SourceKey
    algorithm: ObserverAlgorithm
    algorithm_version: str
    native_analysis_version: str
    generated_at_unix_seconds: float
    source_fingerprint: str
    calibration: CalibrationProfile
    status: Literal["complete", "incomplete"]
    completeness: ObserverCompleteness
    pulse_count: int
    transfer_count: int
    total_dose_mj_cm2: float
    average_pulse_dose_mj_cm2: float
    graph_filename: str
    issues: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "schema_version": OBSERVER_ANALYSIS_SCHEMA_VERSION,
            "role": "observer",
            "run_id": str(self.run_id),
            "session_id": str(self.session_id),
            "source_kind": self.source_key.source_kind,
            "source_id": self.source_key.source_id,
            "algorithm": self.algorithm,
            "algorithm_version": self.algorithm_version,
            "native_analysis_version": self.native_analysis_version,
            "generated_at_unix_seconds": self.generated_at_unix_seconds,
            "source_fingerprint": self.source_fingerprint,
            "calibration": self.calibration.to_dict(),
            "status": self.status,
            "completeness": self.completeness.to_dict(),
            "pulse_count": self.pulse_count,
            "transfer_count": self.transfer_count,
            "total_dose_mj_cm2": self.total_dose_mj_cm2,
            "average_pulse_dose_mj_cm2": self.average_pulse_dose_mj_cm2,
            "graph_filename": self.graph_filename,
            "issues": list(self.issues),
        }

    @classmethod
    def from_dict(cls, value: object) -> "ObserverDoseAnalysis":
        expected = {
            "schema_version",
            "role",
            "run_id",
            "session_id",
            "source_kind",
            "source_id",
            "algorithm",
            "algorithm_version",
            "native_analysis_version",
            "generated_at_unix_seconds",
            "source_fingerprint",
            "calibration",
            "status",
            "completeness",
            "pulse_count",
            "transfer_count",
            "total_dose_mj_cm2",
            "average_pulse_dose_mj_cm2",
            "graph_filename",
            "issues",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("Observer dose analysis contains unknown or missing fields.")
        if value["schema_version"] != OBSERVER_ANALYSIS_SCHEMA_VERSION or value["role"] != "observer":
            raise ValueError("Observer dose analysis schema or role is invalid.")
        algorithm = _require_algorithm(value["algorithm"])
        status = value["status"]
        if status not in {"complete", "incomplete"}:
            raise ValueError("Observer dose analysis status is invalid.")
        issues = value["issues"]
        if not isinstance(issues, list) or not all(isinstance(item, str) for item in issues):
            raise ValueError("Observer dose analysis issues must be text values.")
        pulse_count = _non_negative_int(value["pulse_count"], "pulse_count")
        transfer_count = _non_negative_int(value["transfer_count"], "transfer_count")
        fingerprint = str(value["source_fingerprint"])
        if len(fingerprint) != 64 or any(character not in "0123456789abcdef" for character in fingerprint):
            raise ValueError("Observer dose analysis fingerprint is invalid.")
        result = cls(
            run_id=uuid.UUID(str(value["run_id"])),
            session_id=uuid.UUID(str(value["session_id"])),
            source_key=SourceKey(str(value["source_kind"]), str(value["source_id"])),
            algorithm=algorithm,
            algorithm_version=_non_empty_text(value["algorithm_version"], "algorithm_version"),
            native_analysis_version=_non_empty_text(value["native_analysis_version"], "native_analysis_version"),
            generated_at_unix_seconds=_finite(value["generated_at_unix_seconds"], "generated_at_unix_seconds"),
            source_fingerprint=fingerprint,
            calibration=CalibrationProfile.from_dict(value["calibration"]),
            status=status,
            completeness=ObserverCompleteness.from_dict(value["completeness"]),
            pulse_count=pulse_count,
            transfer_count=transfer_count,
            total_dose_mj_cm2=_finite(value["total_dose_mj_cm2"], "total_dose_mj_cm2"),
            average_pulse_dose_mj_cm2=_finite(
                value["average_pulse_dose_mj_cm2"],
                "average_pulse_dose_mj_cm2",
            ),
            graph_filename=str(value["graph_filename"]),
            issues=tuple(issues),
        )
        if result.graph_filename != observer_graph_filename(result.session_id, result.algorithm):
            raise ValueError("Observer dose analysis graph filename is invalid.")
        if result.transfer_count != result.completeness.included_snapshot_count:
            raise ValueError("Observer dose analysis transfer count is inconsistent.")
        return result


@dataclass(frozen=True)
class ObserverDoseGraphLevel:
    wall_unix_ns: np.ndarray
    dose_increment_mj_cm2: np.ndarray
    cumulative_dose_mj_cm2: np.ndarray
    source_sequence: np.ndarray
    represented_pulse_count: np.ndarray

    @property
    def point_count(self) -> int:
        return len(self.wall_unix_ns)


@dataclass(frozen=True)
class ObserverDoseGraph:
    run_id: uuid.UUID
    session_id: uuid.UUID
    source_key: SourceKey
    algorithm: ObserverAlgorithm
    algorithm_version: str
    source_fingerprint: str
    calibration_hash: str
    status: Literal["complete", "incomplete"]
    raw_point_count: int
    full: ObserverDoseGraphLevel
    thumbnail: ObserverDoseGraphLevel


@dataclass(frozen=True)
class ObserverDoseProduct:
    analysis: ObserverDoseAnalysis
    graph: ObserverDoseGraph


@dataclass(frozen=True)
class _SnapshotInput:
    contents: SnapshotContents
    context: dict
    sha256: str


@dataclass(frozen=True)
class _GraphPoint:
    wall_unix_ns: int
    dose_increment_mj_cm2: float
    source_sequence: int
    represented_pulse_count: int


def write_observer_dose_products(
    entry: Any,
    data_path: str | Path,
    run: Any,
    selected_calibration: CalibrationProfile,
    snapshot_ids: list[uuid.UUID],
    context: dict,
    *,
    expected_native_analysis_version: str,
) -> tuple[ObserverDoseProduct, ObserverDoseProduct]:
    snapshots = _load_snapshot_inputs(
        entry,
        data_path,
        run,
        snapshot_ids,
        context,
        expected_native_analysis_version,
    )
    context_digest = hashlib.sha256(
        json.dumps(context, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    inputs = {
        "run_id": str(run.run_id),
        "session_id": str(run.session_id),
        "source_kind": run.source_key.source_kind,
        "source_id": run.source_key.source_id,
        "native_analysis_version": expected_native_analysis_version,
        "context_sha256": context_digest,
        "snapshots": [
            {"snapshot_id": str(item.contents.snapshot_id), "sha256": item.sha256}
            for item in snapshots
        ],
    }
    captured = _captured_product(run, selected_calibration, snapshots, inputs)
    legacy = _legacy_product(run, snapshots, inputs)
    for product in (captured, legacy):
        graph_path = _entry_folder(data_path, entry) / product.analysis.graph_filename
        _write_graph(graph_path, product.graph)
        entry.register_existing_resource(product.analysis.graph_filename, OBSERVER_GRAPH_RESOURCE_TYPE)
        analysis_name = observer_analysis_filename(run.session_id, product.analysis.algorithm)
        _write_json(_entry_folder(data_path, entry) / analysis_name, product.analysis.to_dict())
        entry.register_existing_resource(analysis_name, OBSERVER_ANALYSIS_RESOURCE_TYPE)
    return captured, legacy


def load_observer_dose_products(
    entry: Any,
    data_path: str | Path,
    run_id: uuid.UUID,
) -> tuple[ObserverDoseProduct, ...]:
    products = []
    resources = dict(entry.list_resources())
    folder = _entry_folder(data_path, entry)
    for name, resource_type in sorted(resources.items()):
        if resource_type != OBSERVER_ANALYSIS_RESOURCE_TYPE:
            continue
        with entry.resource(name, resource_type, "r") as resource:
            analysis = ObserverDoseAnalysis.from_dict(json.load(resource))
        if analysis.run_id != run_id:
            raise ValueError("Observer dose analysis belongs to another run.")
        if name != observer_analysis_filename(analysis.session_id, analysis.algorithm):
            raise ValueError("Observer dose analysis filename is invalid.")
        if resources.get(analysis.graph_filename) != OBSERVER_GRAPH_RESOURCE_TYPE:
            raise ValueError("Observer dose analysis graph is missing or has the wrong type.")
        graph = read_observer_graph_path(folder / analysis.graph_filename)
        if (
            graph.run_id != analysis.run_id
            or graph.session_id != analysis.session_id
            or graph.source_key != analysis.source_key
            or graph.algorithm != analysis.algorithm
            or graph.algorithm_version != analysis.algorithm_version
            or graph.source_fingerprint != analysis.source_fingerprint
            or graph.calibration_hash != analysis.calibration.content_hash
            or graph.status != analysis.status
        ):
            raise ValueError("Observer dose graph provenance does not match its analysis.")
        if not math.isclose(
            float(graph.full.cumulative_dose_mj_cm2[-1]),
            analysis.total_dose_mj_cm2,
            rel_tol=1e-12,
            abs_tol=1e-15,
        ):
            raise ValueError("Observer dose graph total does not match its analysis.")
        products.append(ObserverDoseProduct(analysis, graph))
    return tuple(products)


def read_observer_graph_path(path: str | Path) -> ObserverDoseGraph:
    try:
        with h5py.File(path, "r") as source:
            required = {
                "schema_version",
                "run_id",
                "session_id",
                "source_kind",
                "source_id",
                "algorithm",
                "algorithm_version",
                "graph_algorithm_version",
                "source_fingerprint",
                "calibration_hash",
                "status",
                "raw_point_count",
            }
            if set(source.keys()) != {"full", "thumbnail"} or set(source.attrs) != required:
                raise ValueError("Observer dose graph contains unknown or missing fields.")
            if int(source.attrs["schema_version"]) != OBSERVER_GRAPH_SCHEMA_VERSION:
                raise ValueError("Observer dose graph schema version is unsupported.")
            if _text(source.attrs["graph_algorithm_version"]) != OBSERVER_GRAPH_ALGORITHM_VERSION:
                raise ValueError("Observer dose graph algorithm version is unsupported.")
            algorithm = _require_algorithm(_text(source.attrs["algorithm"]))
            status = _text(source.attrs["status"])
            if status not in {"complete", "incomplete"}:
                raise ValueError("Observer dose graph status is invalid.")
            graph = ObserverDoseGraph(
                run_id=uuid.UUID(_text(source.attrs["run_id"])),
                session_id=uuid.UUID(_text(source.attrs["session_id"])),
                source_key=SourceKey(_text(source.attrs["source_kind"]), _text(source.attrs["source_id"])),
                algorithm=algorithm,
                algorithm_version=_non_empty_text(_text(source.attrs["algorithm_version"]), "algorithm_version"),
                source_fingerprint=_text(source.attrs["source_fingerprint"]),
                calibration_hash=_text(source.attrs["calibration_hash"]),
                status=status,
                raw_point_count=_non_negative_int(int(source.attrs["raw_point_count"]), "raw_point_count"),
                full=_read_graph_level(source["full"], FULL_POINT_LIMIT),
                thumbnail=_read_graph_level(source["thumbnail"], THUMBNAIL_POINT_LIMIT),
            )
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Observer dose graph is invalid: {exc}") from exc
    for fingerprint in (graph.source_fingerprint, graph.calibration_hash):
        if len(fingerprint) != 64 or any(character not in "0123456789abcdef" for character in fingerprint):
            raise ValueError("Observer dose graph hash attribute is invalid.")
    if not math.isclose(
        float(graph.full.cumulative_dose_mj_cm2[-1]),
        float(graph.thumbnail.cumulative_dose_mj_cm2[-1]),
        rel_tol=1e-12,
        abs_tol=1e-15,
    ):
        raise ValueError("Observer dose graph levels have different totals.")
    return graph


def _captured_product(
    run: Any,
    calibration: CalibrationProfile,
    snapshots: tuple[_SnapshotInput, ...],
    inputs: dict,
) -> ObserverDoseProduct:
    points = []
    total = 0.0
    pulse_count = 0
    for snapshot in snapshots:
        contents = snapshot.contents
        for sequence, captured_at, integral in zip(
            contents.sequence,
            contents.captured_at_unix_ns,
            contents.integral_volt_seconds,
        ):
            dose = max(0.0, calibration.dose_for_integral(float(integral)))
            total += dose
            pulse_count += 1
            points.append(_GraphPoint(int(captured_at), dose, int(sequence), 1))
    average = total / pulse_count if pulse_count else 0.0
    completeness = ObserverCompleteness(len(snapshots), len(snapshots), 0, 0, 0)
    return _product(
        run,
        CAPTURED_ALGORITHM,
        CAPTURED_ANALYSIS_VERSION,
        calibration,
        "complete",
        completeness,
        pulse_count,
        len(snapshots),
        total,
        average,
        (),
        points,
        inputs,
    )


def _legacy_product(
    run: Any,
    snapshots: tuple[_SnapshotInput, ...],
    inputs: dict,
) -> ObserverDoseProduct:
    calibration = legacy_default_profile()
    points = []
    total = 0.0
    pulse_dose_total = 0.0
    pulse_count = 0
    included = 0
    excluded = 0
    unknown_eligibility = 0
    unknown_step = 0
    issues = []
    for snapshot in snapshots:
        contents = snapshot.contents
        eligibility = _eligibility_state(snapshot.context)
        if eligibility == "ineligible":
            excluded += 1
            continue
        included += 1
        if eligibility == "unknown":
            unknown_eligibility += 1
        step_mode = _step_mode(snapshot.context)
        if step_mode is None:
            unknown_step += 1
        exposure_start = _exposure_start(snapshot.context)
        pulse_doses = np.asarray(
            [legacy_siglent_dose_for_integral(float(value)) for value in contents.integral_volt_seconds],
            dtype=float,
        )
        pulse_times = np.asarray(contents.captured_at_unix_ns, dtype=float) / 1_000_000_000.0
        source_batch = contents.source_batch
        if source_batch is None:
            raise ValueError("Observer snapshot is missing its source batch envelope.")
        analysis = analyze_legacy_siglent_values(
            source_batch.capture_started_unix_ns,
            source_batch.capture_completed_unix_ns,
            pulse_times,
            np.arange(len(pulse_times), dtype=int),
            pulse_doses,
            contents.maximum_volts,
            is_step_exposure=step_mode,
            exposure_start_ns=exposure_start,
        )
        total += analysis.total_dose_mj_cm2
        for pulse_dose in analysis.pulse_doses_mj_cm2:
            pulse_dose_total += float(pulse_dose)
        pulse_count += len(analysis.pulse_doses_mj_cm2)
        points.append(
            _GraphPoint(
                source_batch.capture_completed_unix_ns,
                analysis.total_dose_mj_cm2,
                int(contents.sequence[-1]),
                len(contents.sequence),
            )
        )
    if unknown_eligibility:
        issues.append(
            f"{unknown_eligibility} transfer(s) lack a definitive legacy laser-off exclusion decision."
        )
    if unknown_step:
        issues.append(f"{unknown_step} transfer(s) use the legacy inferred step mode.")
    status = "incomplete" if issues else "complete"
    completeness = ObserverCompleteness(
        len(snapshots),
        included,
        excluded,
        unknown_eligibility,
        unknown_step,
    )
    return _product(
        run,
        LEGACY_COMPENSATED_ALGORITHM,
        LEGACY_ANALYSIS_VERSION,
        calibration,
        status,
        completeness,
        pulse_count,
        included,
        total,
        pulse_dose_total / pulse_count if pulse_count else 0.0,
        tuple(issues),
        points,
        inputs,
    )


def _product(
    run: Any,
    algorithm: ObserverAlgorithm,
    algorithm_version: str,
    calibration: CalibrationProfile,
    status: Literal["complete", "incomplete"],
    completeness: ObserverCompleteness,
    pulse_count: int,
    transfer_count: int,
    total: float,
    average: float,
    issues: tuple[str, ...],
    points: list[_GraphPoint],
    inputs: dict,
) -> ObserverDoseProduct:
    fingerprint_value = inputs | {
        "algorithm": algorithm,
        "algorithm_version": algorithm_version,
        "calibration_hash": calibration.content_hash,
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    generated_at = time.time()
    graph_name = observer_graph_filename(run.session_id, algorithm)
    analysis = ObserverDoseAnalysis(
        run_id=run.run_id,
        session_id=run.session_id,
        source_key=run.source_key,
        algorithm=algorithm,
        algorithm_version=algorithm_version,
        native_analysis_version=inputs["native_analysis_version"],
        generated_at_unix_seconds=generated_at,
        source_fingerprint=fingerprint,
        calibration=calibration,
        status=status,
        completeness=completeness,
        pulse_count=pulse_count,
        transfer_count=transfer_count,
        total_dose_mj_cm2=total,
        average_pulse_dose_mj_cm2=average,
        graph_filename=graph_name,
        issues=issues,
    )
    origin = run.capture_started_unix_ns
    if points:
        origin = min(origin, points[0].wall_unix_ns) if origin > 0 else points[0].wall_unix_ns
    graph = ObserverDoseGraph(
        run_id=run.run_id,
        session_id=run.session_id,
        source_key=run.source_key,
        algorithm=algorithm,
        algorithm_version=algorithm_version,
        source_fingerprint=fingerprint,
        calibration_hash=calibration.content_hash,
        status=status,
        raw_point_count=len(points),
        full=_graph_level(points, origin, FULL_POINT_LIMIT),
        thumbnail=_graph_level(points, origin, THUMBNAIL_POINT_LIMIT),
    )
    return ObserverDoseProduct(analysis, graph)


def _load_snapshot_inputs(
    entry: Any,
    data_path: str | Path,
    run: Any,
    snapshot_ids: list[uuid.UUID],
    context: dict,
    expected_native_analysis_version: str,
) -> tuple[_SnapshotInput, ...]:
    expected_context = {"schema_version", "run_id", "session_id", "source_kind", "source_id", "snapshots"}
    if not isinstance(context, dict) or set(context) != expected_context or not isinstance(context["snapshots"], list):
        raise ValueError("Observer context contains unknown or missing fields.")
    identity = {
        "run_id": str(run.run_id),
        "session_id": str(run.session_id),
        "source_kind": run.source_key.source_kind,
        "source_id": run.source_key.source_id,
    }
    if any(context.get(name) != value for name, value in identity.items()):
        raise ValueError("Observer context belongs to another capture.")
    context_by_id = {}
    for item in context["snapshots"]:
        if not isinstance(item, dict) or not isinstance(item.get("snapshot_id"), str):
            raise ValueError("Observer snapshot context is invalid.")
        if item["snapshot_id"] in context_by_id:
            raise ValueError("Observer context contains duplicate snapshots.")
        context_by_id[item["snapshot_id"]] = item
    if set(context_by_id) != {str(snapshot_id) for snapshot_id in snapshot_ids}:
        raise ValueError("Observer context does not exactly match the finalized snapshots.")

    folder = _entry_folder(data_path, entry)
    resources = dict(entry.list_resources())
    loaded = []
    expected_sequence = None
    previous_capture = None
    for snapshot_id in snapshot_ids:
        name = f"snap_{snapshot_id}.h5"
        if resources.get(name) != "euv_snapshot":
            raise ValueError(f"Observer snapshot {snapshot_id} is missing or has the wrong type.")
        path = folder / name
        contents = read_snapshot(path)
        if contents.snapshot_id != snapshot_id or contents.session_id != run.session_id:
            raise ValueError("Observer snapshot identity does not match its analysis session.")
        if (contents.source_kind, contents.source_id) != (
            run.source_key.source_kind,
            run.source_key.source_id,
        ):
            raise ValueError("Observer snapshot belongs to another source.")
        if contents.native_analysis_version != expected_native_analysis_version:
            raise ValueError("Observer snapshot has the wrong native analysis version.")
        if contents.source_batch is None:
            raise ValueError("Observer snapshot has no source batch envelope.")
        item = context_by_id[str(snapshot_id)]
        source_batch = contents.source_batch
        for key, expected in (
            ("capture_batch_id", str(source_batch.batch_id)),
            ("capture_batch_kind", source_batch.batch_kind),
            ("capture_started_unix_ns", source_batch.capture_started_unix_ns),
            ("capture_completed_unix_ns", source_batch.capture_completed_unix_ns),
        ):
            if item.get(key) != expected:
                raise ValueError("Observer snapshot context does not match its source batch envelope.")
        if expected_sequence is not None and int(contents.sequence[0]) != expected_sequence:
            raise ValueError("Observer snapshot sequences are not contiguous across transfers.")
        expected_sequence = int(contents.sequence[-1]) + 1
        first_capture = int(contents.captured_at_unix_ns[0])
        if previous_capture is not None and first_capture < previous_capture:
            raise ValueError("Observer pulse timestamps decrease across transfers.")
        previous_capture = int(contents.captured_at_unix_ns[-1])
        loaded.append(_SnapshotInput(contents, item, _sha256(path)))
    return tuple(loaded)


def _graph_level(points: list[_GraphPoint], origin: int, limit: int) -> ObserverDoseGraphLevel:
    ordered = sorted(points, key=lambda item: (item.wall_unix_ns, item.source_sequence))
    group_count = min(len(ordered), limit - 1)
    boundaries = np.linspace(0, len(ordered), group_count + 1, dtype=int) if group_count else np.asarray([0])
    walls = [origin]
    increments = [0.0]
    cumulative = [0.0]
    sequences = [-1]
    represented = [0]
    total = 0.0
    for start, stop in zip(boundaries[:-1], boundaries[1:]):
        if stop <= start:
            continue
        bucket = ordered[start:stop]
        increment = float(sum(item.dose_increment_mj_cm2 for item in bucket))
        total += increment
        final = bucket[-1]
        walls.append(final.wall_unix_ns)
        increments.append(increment)
        cumulative.append(total)
        sequences.append(final.source_sequence)
        represented.append(sum(item.represented_pulse_count for item in bucket))
    return ObserverDoseGraphLevel(
        wall_unix_ns=np.asarray(walls, dtype=np.int64),
        dose_increment_mj_cm2=np.asarray(increments, dtype=np.float64),
        cumulative_dose_mj_cm2=np.asarray(cumulative, dtype=np.float64),
        source_sequence=np.asarray(sequences, dtype=np.int64),
        represented_pulse_count=np.asarray(represented, dtype=np.int64),
    )


def _write_graph(path: Path, graph: ObserverDoseGraph) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with h5py.File(temporary, "w") as destination:
            destination.attrs["schema_version"] = OBSERVER_GRAPH_SCHEMA_VERSION
            destination.attrs["run_id"] = str(graph.run_id)
            destination.attrs["session_id"] = str(graph.session_id)
            destination.attrs["source_kind"] = graph.source_key.source_kind
            destination.attrs["source_id"] = graph.source_key.source_id
            destination.attrs["algorithm"] = graph.algorithm
            destination.attrs["algorithm_version"] = graph.algorithm_version
            destination.attrs["graph_algorithm_version"] = OBSERVER_GRAPH_ALGORITHM_VERSION
            destination.attrs["source_fingerprint"] = graph.source_fingerprint
            destination.attrs["calibration_hash"] = graph.calibration_hash
            destination.attrs["status"] = graph.status
            destination.attrs["raw_point_count"] = graph.raw_point_count
            for name, level in (("full", graph.full), ("thumbnail", graph.thumbnail)):
                group = destination.create_group(name)
                for field in (
                    "wall_unix_ns",
                    "dose_increment_mj_cm2",
                    "cumulative_dose_mj_cm2",
                    "source_sequence",
                    "represented_pulse_count",
                ):
                    group.create_dataset(field, data=getattr(level, field), compression="gzip", shuffle=True)
            destination.flush()
        with temporary.open("r+b") as output:
            os.fsync(output.fileno())
        read_observer_graph_path(temporary)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_graph_level(group: h5py.Group, limit: int) -> ObserverDoseGraphLevel:
    fields = {
        "wall_unix_ns",
        "dose_increment_mj_cm2",
        "cumulative_dose_mj_cm2",
        "source_sequence",
        "represented_pulse_count",
    }
    if set(group.keys()) != fields:
        raise ValueError("Observer dose graph level contains unknown or missing datasets.")
    values = {name: np.asarray(group[name][:]) for name in fields}
    length = len(values["wall_unix_ns"])
    if length < 1 or length > limit or any(value.ndim != 1 or len(value) != length for value in values.values()):
        raise ValueError("Observer dose graph level datasets have invalid shapes.")
    if any(not np.isfinite(values[name]).all() for name in ("dose_increment_mj_cm2", "cumulative_dose_mj_cm2")):
        raise ValueError("Observer dose graph level contains non-finite values.")
    if np.any(np.diff(values["wall_unix_ns"]) < 0):
        raise ValueError("Observer dose graph wall timestamps decrease.")
    if values["dose_increment_mj_cm2"][0] != 0 or values["cumulative_dose_mj_cm2"][0] != 0:
        raise ValueError("Observer dose graph level has no zero baseline.")
    if not np.allclose(
        np.cumsum(values["dose_increment_mj_cm2"]),
        values["cumulative_dose_mj_cm2"],
        rtol=1e-12,
        atol=1e-15,
    ):
        raise ValueError("Observer dose graph cumulative values are inconsistent.")
    if np.any(values["represented_pulse_count"] < 0):
        raise ValueError("Observer dose graph represented pulse count is invalid.")
    return ObserverDoseGraphLevel(**values)


def _write_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as output:
            json.dump(value, output, allow_nan=False, sort_keys=True, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _entry_folder(data_path: str | Path, entry: Any) -> Path:
    foldername = entry.get_foldername()
    if not isinstance(foldername, str) or not foldername or Path(foldername).name != foldername:
        raise ValueError("Observer run entry has an invalid resource folder.")
    root = Path(data_path).resolve()
    folder = (root / foldername).resolve()
    if root != folder and root not in folder.parents:
        raise ValueError("Observer run entry resource folder is outside the data root.")
    return folder


def _eligibility_state(context: dict) -> str:
    value = context.get("laser_off_eligibility")
    if not isinstance(value, dict) or value.get("state") not in {"eligible", "ineligible", "unknown"}:
        raise ValueError("Observer laser-off eligibility context is invalid.")
    return value["state"]


def _step_mode(context: dict) -> bool | None:
    value = context.get("is_step_exposure")
    if value == {"state": "unknown"}:
        return None
    if isinstance(value, dict) and set(value) == {"state", "value"} and value["state"] == "value" and isinstance(value["value"], bool):
        return value["value"]
    raise ValueError("Observer step-mode context is invalid.")


def _exposure_start(context: dict):
    value = context.get("exposure_start_ns")
    if value == {"state": "null"}:
        return None
    if isinstance(value, dict) and set(value) == {"state", "value"} and value["state"] == "value":
        return _non_negative_int(value["value"], "exposure_start_ns")
    if value == {"state": "unknown"}:
        return EXPOSURE_START_UNSPECIFIED
    raise ValueError("Observer exposure-start context is invalid.")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_algorithm(value: object) -> ObserverAlgorithm:
    if value not in {CAPTURED_ALGORITHM, LEGACY_COMPENSATED_ALGORITHM}:
        raise ValueError("Observer dose algorithm is invalid.")
    return value


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"Observer dose analysis {name} must be finite.")
    return float(value)


def _non_negative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"Observer dose analysis {name} must be a non-negative integer.")
    return value


def _non_empty_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Observer dose analysis {name} must be non-empty text.")
    return value


def _text(value: object) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)