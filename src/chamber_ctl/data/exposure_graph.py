from __future__ import annotations

import hashlib
import io
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
import portalocker

from chamber_ctl.data.dose_analysis import (
    CAPTURE_TIMELINE_RESOURCE,
    CALIBRATION_PROVENANCE_RESOURCE,
    HDF5_SNAPSHOT_RESOURCE_TYPE,
    CaptureTimelinePoint,
    analyze_legacy_snapshot,
    hdf5_snapshot_session_id,
    load_capture_timeline,
    load_experiment_calibration,
    resolve_authoritative_hdf5_session,
)
from ipi_ecs.subsystems.run_events import RUN_EVENT_RESOURCE, load_run_event_timeline


EXPOSURE_GRAPH_RESOURCE = "euv_exposure_dose_graph.h5"
EXPOSURE_GRAPH_RESOURCE_TYPE = "euv_exposure_dose_graph"
EXPOSURE_GRAPH_SCHEMA_VERSION = 1
EXPOSURE_GRAPH_ALGORITHM_VERSION = "exposure-dose-graph-v1"
EXPOSURE_GRAPH_RUNTIME_BASIS = "controller-transmitting-v1"
FULL_POINT_LIMIT = 10_000
THUMBNAIL_POINT_LIMIT = 1_000
MAX_RUNTIME_GAP_SECONDS = 0.25

_GROUP_DATASETS = frozenset(
    {
        "wall_elapsed_seconds",
        "runtime_seconds",
        "dose_increment_mj_cm2",
        "cumulative_dose_mj_cm2",
        "source_index",
        "source_sequence",
        "represented_pulse_count",
    }
)
_REQUIRED_ATTRIBUTES = frozenset(
    {
        "schema_version",
        "run_id",
        "generated_at_unix_seconds",
        "algorithm_version",
        "runtime_basis",
        "source_fingerprint",
        "raw_pulse_count",
        "final_sequence",
        "calibration_profile_id",
        "calibration_revision",
        "calibration_hash",
        "wall_origin_quality",
        "runtime_quality",
        "issues_json",
    }
)


class ExposureGraphError(RuntimeError):
    pass


class ExposureGraphNotReady(ExposureGraphError):
    pass


class ExposureGraphValidationError(ExposureGraphError):
    pass


@dataclass(frozen=True)
class ExposureGraphLevel:
    wall_elapsed_seconds: np.ndarray
    runtime_seconds: np.ndarray
    dose_increment_mj_cm2: np.ndarray
    cumulative_dose_mj_cm2: np.ndarray
    source_index: np.ndarray
    source_sequence: np.ndarray
    represented_pulse_count: np.ndarray

    @property
    def point_count(self) -> int:
        return len(self.wall_elapsed_seconds)


@dataclass(frozen=True)
class ExposureGraph:
    run_id: uuid.UUID
    generated_at_unix_seconds: float
    source_fingerprint: str
    raw_pulse_count: int
    final_sequence: int | None
    calibration_profile_id: uuid.UUID
    calibration_revision: int
    calibration_hash: str
    wall_origin_quality: str
    runtime_basis: str
    runtime_quality: str
    issues: tuple[str, ...]
    full: ExposureGraphLevel
    thumbnail: ExposureGraphLevel


@dataclass(frozen=True)
class EnsureExposureGraphResult:
    status: Literal["waiting_for_completion", "busy", "existing", "generated", "replaced"]
    graph: ExposureGraph | None


@dataclass(frozen=True)
class _RawPulse:
    wall_unix_ns: int
    monotonic_ns: int | None
    dose_increment_mj_cm2: float
    source_index: int
    source_sequence: int
    snapshot_id: uuid.UUID


@dataclass(frozen=True)
class _SourceGraph:
    graph: ExposureGraph
    source_fingerprint: str


def _decode_attribute(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _entry_folder(data_path: str | Path, entry: Any) -> Path:
    foldername = entry.get_foldername()
    if not isinstance(foldername, str) or not foldername or Path(foldername).name != foldername:
        raise ExposureGraphValidationError("Experiment entry has an invalid resource folder.")
    root = Path(data_path).resolve()
    folder = (root / foldername).resolve()
    if root != folder and root not in folder.parents:
        raise ExposureGraphValidationError("Experiment entry resource folder is outside the data root.")
    return folder


def _resource_bytes(entry: Any, name: str, resource_type: str) -> bytes:
    with entry.resource(name, resource_type, "rb") as resource:
        return resource.read()


def _snapshot_id_from_name(name: str, suffix: str) -> uuid.UUID:
    if not name.startswith("snap_") or not name.endswith(suffix):
        raise ExposureGraphValidationError(f"Snapshot resource has an invalid filename: {name!r}.")
    try:
        return uuid.UUID(name[5:-len(suffix)])
    except ValueError as exc:
        raise ExposureGraphValidationError(f"Snapshot resource has an invalid UUID: {name!r}.") from exc


def _resource_digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _run_event_origin(entry: Any, resources: dict[str, str], source_digests: dict[str, str]) -> tuple[int | None, tuple[Any, ...]]:
    if resources.get(RUN_EVENT_RESOURCE) != "run_event_journal":
        return None, ()
    payload = _resource_bytes(entry, RUN_EVENT_RESOURCE, "run_event_journal")
    source_digests[RUN_EVENT_RESOURCE] = _resource_digest(payload)
    try:
        timeline = load_run_event_timeline(entry)
    except ValueError as exc:
        raise ExposureGraphValidationError(f"Run event journal is invalid: {exc}") from exc
    origin = next(
        (
            event.producer_unix_ns
            for event in sorted(timeline.events, key=lambda item: (item.producer_unix_ns, item.sequence))
            if event.kind == "lifecycle.phase" and event.payload.get("phase") == "PREINIT"
        ),
        None,
    )
    return origin, timeline.events


def _native_snapshot_pulses(
    payload: bytes,
    snapshot_id: uuid.UUID,
    calibration,
    *,
    source_index_start: int,
) -> list[_RawPulse]:
    required = {"sequence", "captured_at_unix_ns", "captured_at_monotonic_ns", "integral_volt_seconds"}
    with h5py.File(io.BytesIO(payload), "r") as snapshot:
        if uuid.UUID(_decode_attribute(snapshot.attrs.get("snapshot_id", ""))) != snapshot_id:
            raise ExposureGraphValidationError("HDF5 snapshot ID does not match its resource filename.")
        if not required.issubset(snapshot.keys()):
            raise ExposureGraphValidationError("HDF5 snapshot is missing graph source datasets.")
        arrays = {name: np.asarray(snapshot[name][:]) for name in required}
    count = len(arrays["sequence"])
    if count == 0 or any(values.ndim != 1 or len(values) != count for values in arrays.values()):
        raise ExposureGraphValidationError("HDF5 snapshot graph source datasets have inconsistent shapes.")
    sequence = arrays["sequence"].astype(np.int64, copy=False)
    if not np.array_equal(sequence, np.arange(sequence[0], sequence[0] + count)):
        raise ExposureGraphValidationError("HDF5 snapshot sequences must be contiguous and ordered.")
    integrals = arrays["integral_volt_seconds"].astype(np.float64, copy=False)
    if not np.isfinite(integrals).all():
        raise ExposureGraphValidationError("HDF5 snapshot pulse integrals must be finite.")
    capture_unix = arrays["captured_at_unix_ns"].astype(np.int64, copy=False)
    capture_monotonic = arrays["captured_at_monotonic_ns"].astype(np.int64, copy=False)
    if np.any(capture_unix < 0) or np.any(capture_monotonic < 0) or np.any(np.diff(capture_unix) < 0):
        raise ExposureGraphValidationError("HDF5 snapshot capture timestamps are invalid.")
    return [
        _RawPulse(
            wall_unix_ns=int(capture_unix[index]),
            monotonic_ns=int(capture_monotonic[index]),
            dose_increment_mj_cm2=max(0.0, calibration.dose_for_integral(float(integrals[index]))),
            source_index=source_index_start + index,
            source_sequence=int(sequence[index]),
            snapshot_id=snapshot_id,
        )
        for index in range(count)
    ]


def _legacy_snapshot_pulses(
    payload: bytes,
    metadata_payload: bytes,
    snapshot_id: uuid.UUID,
    calibration,
    *,
    source_index_start: int,
) -> tuple[list[_RawPulse], float, str | None]:
    try:
        metadata = json.loads(metadata_payload.decode("utf-8"))
        with np.load(io.BytesIO(payload), allow_pickle=False) as archive:
            waveform = np.asarray(archive["data"])
            indexes = np.asarray(archive["indexes"])
    except (KeyError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExposureGraphValidationError(f"Legacy snapshot {snapshot_id} is malformed.") from exc
    summary = analyze_legacy_snapshot(
        snapshot_id,
        metadata["start"],
        metadata["end"],
        waveform,
        indexes,
        metadata,
        calibration,
        source_sha256=_resource_digest(payload),
    )
    if len(indexes) == 0:
        return [], summary.runtime_seconds, None
    sample_indexes = indexes[:, 0].astype(int)
    pulse_times = indexes[:, 1].astype(float)
    raw_doses = []
    for position, sample_index in enumerate(sample_indexes):
        stop = sample_indexes[position + 1] if position + 1 < len(sample_indexes) else len(waveform)
        pulse = waveform[sample_index:stop, :2]
        baseline = float(np.mean(pulse[: min(25, len(pulse)), 1]))
        raw_doses.append(max(0.0, calibration.dose_for_integral(float(np.trapezoid(pulse[:, 1] - baseline, pulse[:, 0])))))
    weights = np.asarray(raw_doses, dtype=np.float64)
    if float(weights.sum()) <= 0:
        weights = np.ones(len(weights), dtype=np.float64)
    compensated_total = summary.total_dose_mj_cm2
    issue = None
    if compensated_total < 0:
        compensated_total = 0.0
        issue = (
            f"Legacy snapshot {snapshot_id} had a negative compensated dose total; "
            "its graph contribution was clamped to zero."
        )
    doses = weights / float(weights.sum()) * compensated_total
    start_ns = int(float(metadata["start"]))
    first_pulse_time = float(pulse_times[0])
    return (
        [
            _RawPulse(
                wall_unix_ns=start_ns + int(round((float(pulse_times[index]) - first_pulse_time) * 1e9)),
                monotonic_ns=None,
                dose_increment_mj_cm2=float(doses[index]),
                source_index=source_index_start + index,
                source_sequence=-1,
                snapshot_id=snapshot_id,
            )
            for index in range(len(doses))
        ],
        summary.runtime_seconds,
        issue,
    )


def _apply_native_runtime(
    pulses: list[_RawPulse],
    events: tuple[Any, ...],
    capture_timeline: tuple[CaptureTimelinePoint, ...],
) -> tuple[np.ndarray, str]:
    if not pulses:
        return np.empty(0, dtype=np.float64), "unavailable"
    lifecycle_events = sorted(
        (event for event in events if event.kind == "lifecycle.phase"),
        key=lambda event: (event.producer_unix_ns, event.sequence),
    )
    timing_events = sorted(
        (event for event in events if event.kind == "timing.euv_transmitting"),
        key=lambda event: (event.producer_unix_ns, event.sequence),
    )
    lifecycle_index = 0
    timing_index = 0
    running = False
    transmitting = False
    previous_monotonic: int | None = None
    previous_valid = False
    values = np.zeros(len(pulses), dtype=np.float64)
    elapsed = 0.0
    for index, pulse in enumerate(pulses):
        while lifecycle_index < len(lifecycle_events) and lifecycle_events[lifecycle_index].producer_unix_ns <= pulse.wall_unix_ns:
            phase = lifecycle_events[lifecycle_index].payload.get("phase")
            if phase == "RUNNING":
                running = True
            elif phase in {"STOPPING", "STOPPED"}:
                running = False
            lifecycle_index += 1
        while timing_index < len(timing_events):
            event = timing_events[timing_index]
            applies_by_sequence = event.next_sequence is not None and event.next_sequence <= pulse.source_sequence
            applies_by_wall_time = event.next_sequence is None and event.producer_unix_ns <= pulse.wall_unix_ns
            if not applies_by_sequence and not applies_by_wall_time:
                break
            transmitting = bool(event.payload.get("value"))
            timing_index += 1
        valid = running and transmitting
        if previous_monotonic is not None and previous_valid and valid:
            delta_ns = pulse.monotonic_ns - previous_monotonic if pulse.monotonic_ns is not None else -1
            if 0 <= delta_ns <= int(MAX_RUNTIME_GAP_SECONDS * 1e9):
                elapsed += delta_ns / 1e9
        values[index] = elapsed
        previous_monotonic = pulse.monotonic_ns
        previous_valid = valid

    raw_values = values.copy()
    anchors = {point.final_sequence: point.cumulative_runtime_seconds for point in capture_timeline}
    previous_index = -1
    previous_runtime = 0.0
    anchored = False
    for index, pulse in enumerate(pulses):
        expected_runtime = anchors.get(pulse.source_sequence)
        if expected_runtime is None:
            continue
        if expected_runtime < previous_runtime:
            raise ExposureGraphValidationError("Capture timeline runtime decreases.")
        segment = raw_values[previous_index + 1:index + 1]
        raw_start = raw_values[previous_index] if previous_index >= 0 else 0.0
        increments = np.diff(np.concatenate(([raw_start], segment)))
        target_delta = expected_runtime - previous_runtime
        raw_delta = float(increments.sum())
        if raw_delta > 0:
            scaled = increments * (target_delta / raw_delta)
        else:
            scaled = np.full(len(increments), target_delta / len(increments), dtype=np.float64)
        values[previous_index + 1:index + 1] = previous_runtime + np.cumsum(scaled)
        values[index] = expected_runtime
        previous_index = index
        previous_runtime = expected_runtime
        anchored = True
    return values, "capture_timeline_anchored" if anchored else "reconstructed_events"


def _legacy_runtime(pulses: list[_RawPulse], per_snapshot_runtime: dict[uuid.UUID, float]) -> np.ndarray:
    values = np.zeros(len(pulses), dtype=np.float64)
    start = 0
    cumulative = 0.0
    while start < len(pulses):
        snapshot_id = pulses[start].snapshot_id
        stop = start + 1
        while stop < len(pulses) and pulses[stop].snapshot_id == snapshot_id:
            stop += 1
        runtime = per_snapshot_runtime[snapshot_id]
        fractions = np.arange(1, stop - start + 1, dtype=np.float64) / (stop - start)
        values[start:stop] = cumulative + fractions * runtime
        cumulative += runtime
        start = stop
    return values


def _graph_level(pulses: list[_RawPulse], wall_elapsed: np.ndarray, runtime: np.ndarray, limit: int) -> ExposureGraphLevel:
    if limit < 2:
        raise ValueError("Exposure graph point limit must be at least two.")
    if len(pulses) != len(wall_elapsed) or len(pulses) != len(runtime):
        raise ExposureGraphValidationError("Exposure graph point arrays are inconsistent.")
    group_count = min(len(pulses), limit - 1)
    boundaries = np.linspace(0, len(pulses), group_count + 1, dtype=int)
    walls = [0.0]
    runtimes = [0.0]
    increments = [0.0]
    cumulative = [0.0]
    source_indexes = [-1]
    source_sequences = [-1]
    represented = [0]
    total = 0.0
    for start, stop in zip(boundaries[:-1], boundaries[1:]):
        if stop <= start:
            continue
        bucket = pulses[start:stop]
        increment = float(sum(item.dose_increment_mj_cm2 for item in bucket))
        total += increment
        final = bucket[-1]
        walls.append(float(wall_elapsed[stop - 1]))
        runtimes.append(float(runtime[stop - 1]))
        increments.append(increment)
        cumulative.append(total)
        source_indexes.append(final.source_index)
        source_sequences.append(final.source_sequence)
        represented.append(stop - start)
    return ExposureGraphLevel(
        wall_elapsed_seconds=np.asarray(walls, dtype=np.float64),
        runtime_seconds=np.asarray(runtimes, dtype=np.float64),
        dose_increment_mj_cm2=np.asarray(increments, dtype=np.float64),
        cumulative_dose_mj_cm2=np.asarray(cumulative, dtype=np.float64),
        source_index=np.asarray(source_indexes, dtype=np.int64),
        source_sequence=np.asarray(source_sequences, dtype=np.int64),
        represented_pulse_count=np.asarray(represented, dtype=np.int64),
    )


def _active_analysis_totals(entry: Any, resources: dict[str, str], source_digests: dict[str, str]) -> tuple[float | None, float | None, str | None]:
    analysis_id = entry.get_tags().get("active_dose_analysis")
    if analysis_id is None:
        return None, None, None
    try:
        normalized = str(uuid.UUID(str(analysis_id)))
    except ValueError as exc:
        raise ExposureGraphValidationError("Active dose analysis tag is not a UUID.") from exc
    name = f"dose_analysis_{normalized}.json"
    if resources.get(name) != "dose_analysis":
        raise ExposureGraphValidationError("Active dose analysis resource is missing.")
    payload = _resource_bytes(entry, name, "dose_analysis")
    source_digests[name] = _resource_digest(payload)
    try:
        value = json.loads(payload.decode("utf-8"))
        return float(value["total_dose_mj_cm2"]), float(value["runtime_seconds"]), normalized
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExposureGraphValidationError("Active dose analysis resource is invalid.") from exc


def _source_graph(run_id: uuid.UUID, entry: Any) -> _SourceGraph:
    resources = dict(entry.list_resources())
    calibration = load_experiment_calibration(entry)
    try:
        authoritative_session = resolve_authoritative_hdf5_session(entry, resources)
    except ValueError as exc:
        raise ExposureGraphValidationError(f"Authoritative capture session is invalid: {exc}") from exc
    source_digests: dict[str, str] = {}
    if CALIBRATION_PROVENANCE_RESOURCE in resources:
        source_digests[CALIBRATION_PROVENANCE_RESOURCE] = _resource_digest(
            _resource_bytes(entry, CALIBRATION_PROVENANCE_RESOURCE, resources[CALIBRATION_PROVENANCE_RESOURCE])
        )
    event_origin, events = _run_event_origin(entry, resources, source_digests)
    timeline_payload = None
    if CAPTURE_TIMELINE_RESOURCE in resources:
        timeline_payload = _resource_bytes(entry, CAPTURE_TIMELINE_RESOURCE, resources[CAPTURE_TIMELINE_RESOURCE])
        source_digests[CAPTURE_TIMELINE_RESOURCE] = _resource_digest(timeline_payload)
    try:
        capture_timeline = load_capture_timeline(entry)
    except ValueError as exc:
        raise ExposureGraphValidationError(f"Capture timeline is invalid: {exc}") from exc
    expected_dose, expected_runtime, active_analysis_id = _active_analysis_totals(entry, resources, source_digests)
    if "end_metadata.json" in resources:
        source_digests["end_metadata.json"] = _resource_digest(_resource_bytes(entry, "end_metadata.json", resources["end_metadata.json"]))

    native: list[_RawPulse] = []
    legacy: list[_RawPulse] = []
    legacy_runtime: dict[uuid.UUID, float] = {}
    legacy_dose_clamped = False
    issues: list[str] = []
    source_index = 0
    for name, resource_type in sorted(resources.items()):
        if resource_type == HDF5_SNAPSHOT_RESOURCE_TYPE and name.endswith(".h5"):
            snapshot_id = _snapshot_id_from_name(name, ".h5")
            payload = _resource_bytes(entry, name, resource_type)
            try:
                session_id = hdf5_snapshot_session_id(payload, filename=name)
            except ValueError as exc:
                raise ExposureGraphValidationError(str(exc)) from exc
            if session_id != authoritative_session:
                continue
            source_digests[name] = _resource_digest(payload)
            snapshot_pulses = _native_snapshot_pulses(payload, snapshot_id, calibration, source_index_start=source_index)
            source_index += len(snapshot_pulses)
            native.extend(snapshot_pulses)
        elif resource_type == "snapshot" and name.endswith(".npz"):
            snapshot_id = _snapshot_id_from_name(name, ".npz")
            metadata_name = f"snap_{snapshot_id}.json"
            if resources.get(metadata_name) != "snap_meta":
                raise ExposureGraphValidationError(f"Legacy snapshot {snapshot_id} is missing metadata.")
            payload = _resource_bytes(entry, name, resource_type)
            metadata_payload = _resource_bytes(entry, metadata_name, "snap_meta")
            source_digests[name] = _resource_digest(payload)
            source_digests[metadata_name] = _resource_digest(metadata_payload)
            snapshot_pulses, snapshot_runtime, legacy_issue = _legacy_snapshot_pulses(
                payload,
                metadata_payload,
                snapshot_id,
                calibration,
                source_index_start=source_index,
            )
            source_index += len(snapshot_pulses)
            legacy.extend(snapshot_pulses)
            legacy_runtime[snapshot_id] = snapshot_runtime
            if legacy_issue is not None:
                issues.append(legacy_issue)
                legacy_dose_clamped = True

    if native and legacy:
        issues.append("Mixed native and legacy snapshots were ordered by capture wall time.")
        pulses = sorted(native + legacy, key=lambda item: (item.wall_unix_ns, item.source_index))
        runtime = np.zeros(len(pulses), dtype=np.float64)
        native_indexes = [index for index, item in enumerate(pulses) if item.monotonic_ns is not None]
        native_values, native_quality = _apply_native_runtime([pulses[index] for index in native_indexes], events, capture_timeline)
        runtime[native_indexes] = native_values
        runtime_quality = f"mixed:{native_quality}:approximate_legacy"
    elif native:
        pulses = sorted(native, key=lambda item: item.source_sequence)
        sequence = [item.source_sequence for item in pulses]
        if len(sequence) > 1 and any(right <= left for left, right in zip(sequence, sequence[1:])):
            raise ExposureGraphValidationError("Native pulse sequences are not strictly increasing across snapshots.")
        if len(sequence) > 1 and any(right != left + 1 for left, right in zip(sequence, sequence[1:])):
            issues.append("Native pulse sequence gaps are present in the persisted graph.")
        runtime, runtime_quality = _apply_native_runtime(pulses, events, capture_timeline)
    else:
        pulses = sorted(legacy, key=lambda item: (item.wall_unix_ns, item.source_index))
        runtime = _legacy_runtime(pulses, legacy_runtime)
        runtime_quality = "approximate_legacy"
    if not pulses:
        raise ExposureGraphValidationError("Exposure has no registered pulse data for graph generation.")

    origin = event_origin if event_origin is not None else pulses[0].wall_unix_ns
    wall_quality = "run_preinit" if event_origin is not None else "first_capture"
    wall_elapsed = np.maximum(0.0, (np.asarray([item.wall_unix_ns for item in pulses], dtype=np.float64) - origin) / 1e9)
    if np.any(np.diff(wall_elapsed) < 0) or np.any(np.diff(runtime) < -1e-12):
        raise ExposureGraphValidationError("Exposure graph coordinates must not decrease.")
    total_dose = float(sum(item.dose_increment_mj_cm2 for item in pulses))
    if expected_dose is not None and not math.isclose(total_dose, expected_dose, rel_tol=1e-7, abs_tol=1e-9):
        if not legacy_dose_clamped:
            raise ExposureGraphValidationError("Pulse graph dose does not match the active dose analysis.")
        issues.append(
            "Graph dose differs from the active analysis because negative legacy compensated totals were clamped to zero."
        )
    if expected_runtime is not None and capture_timeline and not math.isclose(runtime[-1], expected_runtime, rel_tol=1e-7, abs_tol=1e-9):
        raise ExposureGraphValidationError("Pulse graph runtime does not match the active dose analysis.")

    fingerprint_payload = {
        "algorithm_version": EXPOSURE_GRAPH_ALGORITHM_VERSION,
        "runtime_basis": EXPOSURE_GRAPH_RUNTIME_BASIS,
        "run_id": str(run_id),
        "calibration_hash": calibration.content_hash,
        "active_analysis_id": active_analysis_id,
        "resources": dict(sorted(source_digests.items())),
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    graph = ExposureGraph(
        run_id=run_id,
        generated_at_unix_seconds=time.time(),
        source_fingerprint=fingerprint,
        raw_pulse_count=len(pulses),
        final_sequence=None if pulses[-1].source_sequence < 0 else pulses[-1].source_sequence,
        calibration_profile_id=calibration.profile_id,
        calibration_revision=calibration.revision,
        calibration_hash=calibration.content_hash,
        wall_origin_quality=wall_quality,
        runtime_basis=EXPOSURE_GRAPH_RUNTIME_BASIS,
        runtime_quality=runtime_quality,
        issues=tuple(issues),
        full=_graph_level(pulses, wall_elapsed, runtime, FULL_POINT_LIMIT),
        thumbnail=_graph_level(pulses, wall_elapsed, runtime, THUMBNAIL_POINT_LIMIT),
    )
    return _SourceGraph(graph, fingerprint)


def _write_level(group: h5py.Group, level: ExposureGraphLevel) -> None:
    for name in _GROUP_DATASETS:
        group.create_dataset(name, data=getattr(level, name), compression="gzip", shuffle=True)


def _read_level(group: h5py.Group, *, limit: int) -> ExposureGraphLevel:
    if set(group.keys()) != _GROUP_DATASETS:
        raise ExposureGraphValidationError("Exposure graph level has unknown or missing datasets.")
    values = {name: np.asarray(group[name][:]) for name in _GROUP_DATASETS}
    length = len(values["wall_elapsed_seconds"])
    if length < 1 or length > limit or any(value.ndim != 1 or len(value) != length for value in values.values()):
        raise ExposureGraphValidationError("Exposure graph level dataset shapes are invalid.")
    float_names = ("wall_elapsed_seconds", "runtime_seconds", "dose_increment_mj_cm2", "cumulative_dose_mj_cm2")
    if any(not np.isfinite(values[name]).all() for name in float_names):
        raise ExposureGraphValidationError("Exposure graph level contains non-finite values.")
    if np.any(np.diff(values["wall_elapsed_seconds"]) < 0) or np.any(np.diff(values["runtime_seconds"]) < 0):
        raise ExposureGraphValidationError("Exposure graph coordinates decrease.")
    if values["dose_increment_mj_cm2"][0] != 0 or values["cumulative_dose_mj_cm2"][0] != 0:
        raise ExposureGraphValidationError("Exposure graph level is missing its zero baseline.")
    cumulative = np.cumsum(values["dose_increment_mj_cm2"])
    if not np.allclose(cumulative, values["cumulative_dose_mj_cm2"], rtol=1e-9, atol=1e-12):
        raise ExposureGraphValidationError("Exposure graph cumulative dose is inconsistent.")
    if np.any(values["represented_pulse_count"] < 0):
        raise ExposureGraphValidationError("Exposure graph represented pulse count is invalid.")
    return ExposureGraphLevel(**values)


def read_exposure_graph_path(path: str | Path, *, expected_run_id: uuid.UUID | None = None) -> ExposureGraph:
    graph_path = Path(path)
    try:
        with h5py.File(graph_path, "r") as source:
            if set(source.keys()) != {"full", "thumbnail"}:
                raise ExposureGraphValidationError("Exposure graph has unknown or missing levels.")
            if not _REQUIRED_ATTRIBUTES.issubset(source.attrs):
                raise ExposureGraphValidationError("Exposure graph has unknown or missing provenance attributes.")
            if int(source.attrs["schema_version"]) != EXPOSURE_GRAPH_SCHEMA_VERSION:
                raise ExposureGraphValidationError("Unsupported exposure graph schema version.")
            if _decode_attribute(source.attrs["algorithm_version"]) != EXPOSURE_GRAPH_ALGORITHM_VERSION:
                raise ExposureGraphValidationError("Unsupported exposure graph algorithm version.")
            run_id = uuid.UUID(_decode_attribute(source.attrs["run_id"]))
            if expected_run_id is not None and run_id != expected_run_id:
                raise ExposureGraphValidationError("Exposure graph run ID does not match its requested run.")
            issues = json.loads(_decode_attribute(source.attrs["issues_json"]))
            if not isinstance(issues, list) or not all(isinstance(item, str) for item in issues):
                raise ExposureGraphValidationError("Exposure graph issues attribute is invalid.")
            graph = ExposureGraph(
                run_id=run_id,
                generated_at_unix_seconds=float(source.attrs["generated_at_unix_seconds"]),
                source_fingerprint=_decode_attribute(source.attrs["source_fingerprint"]),
                raw_pulse_count=int(source.attrs["raw_pulse_count"]),
                final_sequence=None if int(source.attrs["final_sequence"]) < 0 else int(source.attrs["final_sequence"]),
                calibration_profile_id=uuid.UUID(_decode_attribute(source.attrs["calibration_profile_id"])),
                calibration_revision=int(source.attrs["calibration_revision"]),
                calibration_hash=_decode_attribute(source.attrs["calibration_hash"]),
                wall_origin_quality=_decode_attribute(source.attrs["wall_origin_quality"]),
                runtime_basis=_decode_attribute(source.attrs["runtime_basis"]),
                runtime_quality=_decode_attribute(source.attrs["runtime_quality"]),
                issues=tuple(issues),
                full=_read_level(source["full"], limit=FULL_POINT_LIMIT),
                thumbnail=_read_level(source["thumbnail"], limit=THUMBNAIL_POINT_LIMIT),
            )
    except (OSError, ValueError, KeyError) as exc:
        if isinstance(exc, ExposureGraphValidationError):
            raise
        raise ExposureGraphValidationError(f"Exposure graph is invalid or unavailable: {exc}") from exc
    if graph.raw_pulse_count < 1 or graph.full.cumulative_dose_mj_cm2[-1] < 0:
        raise ExposureGraphValidationError("Exposure graph aggregate totals are invalid.")
    if not math.isclose(
        float(graph.full.cumulative_dose_mj_cm2[-1]),
        float(graph.thumbnail.cumulative_dose_mj_cm2[-1]),
        rel_tol=1e-9,
        abs_tol=1e-12,
    ):
        raise ExposureGraphValidationError("Exposure graph resolution levels disagree on final dose.")
    return graph


def read_exposure_graph(entry: Any, data_path: str | Path, run_id: uuid.UUID) -> ExposureGraph:
    resources = dict(entry.list_resources())
    if resources.get(EXPOSURE_GRAPH_RESOURCE) != EXPOSURE_GRAPH_RESOURCE_TYPE:
        raise FileNotFoundError("Exposure graph is not registered.")
    return read_exposure_graph_path(_entry_folder(data_path, entry) / EXPOSURE_GRAPH_RESOURCE, expected_run_id=run_id)


def _write_exposure_graph(path: Path, graph: ExposureGraph) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with h5py.File(temporary, "w") as destination:
            destination.attrs["schema_version"] = EXPOSURE_GRAPH_SCHEMA_VERSION
            destination.attrs["run_id"] = str(graph.run_id)
            destination.attrs["generated_at_unix_seconds"] = graph.generated_at_unix_seconds
            destination.attrs["algorithm_version"] = EXPOSURE_GRAPH_ALGORITHM_VERSION
            destination.attrs["runtime_basis"] = graph.runtime_basis
            destination.attrs["source_fingerprint"] = graph.source_fingerprint
            destination.attrs["raw_pulse_count"] = graph.raw_pulse_count
            destination.attrs["final_sequence"] = -1 if graph.final_sequence is None else graph.final_sequence
            destination.attrs["calibration_profile_id"] = str(graph.calibration_profile_id)
            destination.attrs["calibration_revision"] = graph.calibration_revision
            destination.attrs["calibration_hash"] = graph.calibration_hash
            destination.attrs["wall_origin_quality"] = graph.wall_origin_quality
            destination.attrs["runtime_quality"] = graph.runtime_quality
            destination.attrs["issues_json"] = json.dumps(graph.issues, allow_nan=False, separators=(",", ":"))
            _write_level(destination.create_group("full"), graph.full)
            _write_level(destination.create_group("thumbnail"), graph.thumbnail)
            destination.flush()
        with temporary.open("r+b") as output:
            os.fsync(output.fileno())
        read_exposure_graph_path(temporary, expected_run_id=graph.run_id)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _is_terminal(entry: Any) -> bool:
    resources = dict(entry.list_resources())
    return resources.get("end_metadata.json") == "metadata"


def ensure_exposure_graph(
    run_id: uuid.UUID,
    entry: Any,
    data_path: str | Path,
    *,
    allow_incomplete: bool = False,
) -> EnsureExposureGraphResult:
    if not isinstance(run_id, uuid.UUID):
        raise ValueError("Exposure graph run ID must be a UUID.")
    if not allow_incomplete and not _is_terminal(entry):
        return EnsureExposureGraphResult("waiting_for_completion", None)
    folder = _entry_folder(data_path, entry)
    lock_path = Path(data_path).resolve() / ".locks" / "exposure-graphs" / f"{run_id}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with portalocker.Lock(
            lock_path,
            mode="a+b",
            timeout=0,
            flags=portalocker.LockFlags.EXCLUSIVE | portalocker.LockFlags.NON_BLOCKING,
        ):
            source = _source_graph(run_id, entry)
            path = folder / EXPOSURE_GRAPH_RESOURCE
            previous: ExposureGraph | None = None
            try:
                previous = read_exposure_graph_path(path, expected_run_id=run_id)
            except (FileNotFoundError, ExposureGraphValidationError):
                previous = None
            if previous is not None and previous.source_fingerprint == source.source_fingerprint:
                entry.register_existing_resource(EXPOSURE_GRAPH_RESOURCE, EXPOSURE_GRAPH_RESOURCE_TYPE)
                return EnsureExposureGraphResult("existing", previous)
            _write_exposure_graph(path, source.graph)
            entry.register_existing_resource(EXPOSURE_GRAPH_RESOURCE, EXPOSURE_GRAPH_RESOURCE_TYPE)
            return EnsureExposureGraphResult("replaced" if previous is not None else "generated", source.graph)
    except portalocker.exceptions.LockException:
        return EnsureExposureGraphResult("busy", None)