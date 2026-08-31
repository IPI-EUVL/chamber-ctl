from __future__ import annotations

import bisect
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

from chamber_ctl.data.capture_cadence import (
    CadenceQuality,
    GapConfidence,
    PulseCadenceObservation,
    ROLLING_WINDOW_OPTIONS_SECONDS,
    infer_gap,
)
from chamber_ctl.data.dose_analysis import HDF5_SNAPSHOT_RESOURCE_TYPE
from ipi_ecs.subsystems.run_events import RUN_EVENT_RESOURCE, load_run_event_timeline


CAPTURE_CADENCE_GRAPH_RESOURCE = "euv_capture_cadence.h5"
CAPTURE_CADENCE_GRAPH_RESOURCE_TYPE = "euv_capture_cadence"
CAPTURE_SESSION_RESOURCE = "euv_capture_session.json"
CAPTURE_SESSION_RESOURCE_TYPE = "euv_capture_session"
CAPTURE_CADENCE_GRAPH_SCHEMA_VERSION = 1
CAPTURE_CADENCE_GRAPH_ALGORITHM_VERSION = "capture-cadence-timestamp-v1"
MAX_SERIES_POINTS = 10_000

_DATASETS = frozenset(
    {
        "elapsed_seconds",
        "source_sequence",
        "segment_id",
        "rolling_window_seconds",
        "capture_rate_hz",
        "estimated_lost_per_second",
        "gap_elapsed_seconds",
        "gap_sequence_before",
        "gap_sequence_after",
        "gap_interval_seconds",
        "gap_estimated_lost_count",
        "gap_residual_seconds",
        "gap_confidence_high",
        "gap_crosses_snapshot_boundary",
    }
)
_REQUIRED_ATTRIBUTES = frozenset(
    {
        "schema_version",
        "run_id",
        "session_id",
        "generated_at_unix_seconds",
        "algorithm_version",
        "source_fingerprint",
        "expected_rate_hz",
        "quality",
        "raw_capture_count",
        "series_point_count",
        "inferred_lost_count",
        "ambiguous_gap_count",
        "issues_json",
    }
)


class CaptureCadenceGraphError(RuntimeError):
    pass


class CaptureCadenceGraphNotReady(CaptureCadenceGraphError):
    pass


class CaptureCadenceGraphValidationError(CaptureCadenceGraphError):
    pass


@dataclass(frozen=True)
class CaptureCadenceGraph:
    run_id: uuid.UUID
    session_id: uuid.UUID
    generated_at_unix_seconds: float
    source_fingerprint: str
    expected_rate_hz: float
    quality: CadenceQuality
    raw_capture_count: int
    inferred_lost_count: int
    ambiguous_gap_count: int
    issues: tuple[str, ...]
    elapsed_seconds: np.ndarray
    source_sequence: np.ndarray
    segment_id: np.ndarray
    rolling_window_seconds: np.ndarray
    capture_rate_hz: np.ndarray
    estimated_lost_per_second: np.ndarray
    gap_elapsed_seconds: np.ndarray
    gap_sequence_before: np.ndarray
    gap_sequence_after: np.ndarray
    gap_interval_seconds: np.ndarray
    gap_estimated_lost_count: np.ndarray
    gap_residual_seconds: np.ndarray
    gap_confidence_high: np.ndarray
    gap_crosses_snapshot_boundary: np.ndarray


@dataclass(frozen=True)
class EnsureCaptureCadenceGraphResult:
    status: Literal["waiting_for_completion", "busy", "existing", "generated", "replaced"]
    graph: CaptureCadenceGraph | None


@dataclass(frozen=True)
class _RawCapture:
    session_id: uuid.UUID
    sequence: int
    captured_at_unix_ns: int
    captured_at_monotonic_ns: int
    snapshot_id: uuid.UUID


def _decode_attribute(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def _entry_folder(data_path: str | Path, entry: Any) -> Path:
    foldername = entry.get_foldername()
    if not isinstance(foldername, str) or not foldername or Path(foldername).name != foldername:
        raise CaptureCadenceGraphValidationError("Experiment entry has an invalid resource folder.")
    root = Path(data_path).resolve()
    folder = (root / foldername).resolve()
    if root != folder and root not in folder.parents:
        raise CaptureCadenceGraphValidationError("Experiment entry resource folder is outside the data root.")
    return folder


def _resource_bytes(entry: Any, name: str, resource_type: str) -> bytes:
    with entry.resource(name, resource_type, "rb") as resource:
        return resource.read()


def _resource_digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _snapshot_id_from_name(name: str) -> uuid.UUID:
    if not name.startswith("snap_") or not name.endswith(".h5"):
        raise CaptureCadenceGraphValidationError(f"Snapshot resource has an invalid filename: {name!r}.")
    try:
        return uuid.UUID(name[5:-3])
    except ValueError as exc:
        raise CaptureCadenceGraphValidationError(f"Snapshot resource has an invalid UUID: {name!r}.") from exc


def _capture_session(entry: Any, resources: dict[str, str]) -> tuple[uuid.UUID, float, bytes]:
    if resources.get(CAPTURE_SESSION_RESOURCE) != CAPTURE_SESSION_RESOURCE_TYPE:
        raise CaptureCadenceGraphNotReady("Exposure has no native capture-session provenance.")
    payload = _resource_bytes(entry, CAPTURE_SESSION_RESOURCE, CAPTURE_SESSION_RESOURCE_TYPE)
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CaptureCadenceGraphValidationError("Capture-session provenance is invalid JSON.") from exc
    expected = {
        "session_id",
        "calibration_profile_id",
        "calibration_revision",
        "calibration_hash",
        "chopper_frequency_hz",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise CaptureCadenceGraphValidationError("Capture-session provenance has unknown or missing fields.")
    try:
        session_id = uuid.UUID(str(value["session_id"]))
        chopper_frequency_hz = float(value["chopper_frequency_hz"])
    except (TypeError, ValueError, AttributeError) as exc:
        raise CaptureCadenceGraphValidationError("Capture-session cadence values are invalid.") from exc
    if not math.isfinite(chopper_frequency_hz) or chopper_frequency_hz <= 0:
        raise CaptureCadenceGraphValidationError("Capture-session chopper frequency must be positive.")
    return session_id, chopper_frequency_hz / 2.0, payload


def _native_snapshot_captures(payload: bytes, snapshot_id: uuid.UUID) -> list[_RawCapture]:
    required = {"sequence", "captured_at_unix_ns", "captured_at_monotonic_ns"}
    try:
        with h5py.File(io.BytesIO(payload), "r") as snapshot:
            if uuid.UUID(_decode_attribute(snapshot.attrs.get("snapshot_id", ""))) != snapshot_id:
                raise CaptureCadenceGraphValidationError("HDF5 snapshot ID does not match its filename.")
            session_id = uuid.UUID(_decode_attribute(snapshot.attrs.get("session_id", "")))
            if not required.issubset(snapshot.keys()):
                raise CaptureCadenceGraphValidationError("HDF5 snapshot is missing cadence source datasets.")
            arrays = {name: np.asarray(snapshot[name][:]) for name in required}
    except (OSError, ValueError) as exc:
        if isinstance(exc, CaptureCadenceGraphValidationError):
            raise
        raise CaptureCadenceGraphValidationError(f"HDF5 snapshot is invalid: {exc}") from exc
    count = len(arrays["sequence"])
    if count == 0 or any(values.ndim != 1 or len(values) != count for values in arrays.values()):
        raise CaptureCadenceGraphValidationError("HDF5 snapshot cadence datasets have inconsistent shapes.")
    if any(values.dtype.kind not in "iu" for values in arrays.values()):
        raise CaptureCadenceGraphValidationError("HDF5 snapshot cadence datasets must contain integers.")
    sequence = arrays["sequence"].astype(np.int64, copy=False)
    capture_unix = arrays["captured_at_unix_ns"].astype(np.int64, copy=False)
    capture_monotonic = arrays["captured_at_monotonic_ns"].astype(np.int64, copy=False)
    if np.any(sequence < 0) or np.any(capture_unix < 0) or np.any(capture_monotonic < 0):
        raise CaptureCadenceGraphValidationError("HDF5 snapshot cadence values must be non-negative.")
    if np.any(np.diff(sequence) <= 0) or np.any(np.diff(capture_monotonic) <= 0):
        raise CaptureCadenceGraphValidationError("HDF5 snapshot cadence values must increase.")
    return [
        _RawCapture(
            session_id=session_id,
            sequence=int(sequence[index]),
            captured_at_unix_ns=int(capture_unix[index]),
            captured_at_monotonic_ns=int(capture_monotonic[index]),
            snapshot_id=snapshot_id,
        )
        for index in range(count)
    ]


def _trigger_segments(captures: list[_RawCapture], events: tuple[Any, ...]) -> tuple[np.ndarray, tuple[str, ...]]:
    trigger_events = sorted(
        (event for event in events if event.kind == "timing.triggers_enabled"),
        key=lambda event: (event.producer_unix_ns, event.sequence),
    )
    if not trigger_events:
        return np.zeros(len(captures), dtype=np.int64), (
            "Trigger-state timeline was unavailable; all captures were analyzed as one active segment.",
        )

    segments = np.zeros(len(captures), dtype=np.int64)
    event_index = 0
    segment = 0
    break_pending = False
    for capture_index, capture in enumerate(captures):
        while event_index < len(trigger_events):
            event = trigger_events[event_index]
            applies = (
                event.next_sequence <= capture.sequence
                if event.next_sequence is not None
                else event.producer_unix_ns <= capture.captured_at_unix_ns
            )
            if not applies:
                break
            value = event.payload.get("value")
            if not isinstance(value, bool):
                raise CaptureCadenceGraphValidationError("Trigger-state event value must be boolean.")
            if not value:
                break_pending = True
            event_index += 1
        if capture_index > 0 and break_pending:
            segment += 1
            break_pending = False
        segments[capture_index] = segment
    return segments, ()


def _rolling_series(
    captures: list[_RawCapture],
    segments: np.ndarray,
    gaps_by_capture_index: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    windows = np.asarray(ROLLING_WINDOW_OPTIONS_SECONDS, dtype=np.float64)
    capture_rate = np.zeros((len(windows), len(captures)), dtype=np.float64)
    loss_rate = np.zeros_like(capture_rate)
    for segment in np.unique(segments):
        indexes = np.flatnonzero(segments == segment)
        timestamps = [captures[int(index)].captured_at_monotonic_ns for index in indexes]
        losses = gaps_by_capture_index[indexes]
        loss_prefix = np.concatenate(([0], np.cumsum(losses, dtype=np.int64)))
        for local_index, source_index in enumerate(indexes):
            current_ns = timestamps[local_index]
            segment_start_ns = timestamps[0]
            for window_index, window_seconds in enumerate(windows):
                window_start_ns = max(segment_start_ns, current_ns - int(window_seconds * 1e9))
                duration_seconds = (current_ns - window_start_ns) / 1e9
                if duration_seconds <= 0:
                    continue
                left = bisect.bisect_right(timestamps, window_start_ns, hi=local_index + 1)
                captured = local_index - left + 1
                lost = int(loss_prefix[local_index + 1] - loss_prefix[left])
                capture_rate[window_index, source_index] = captured / duration_seconds
                loss_rate[window_index, source_index] = lost / duration_seconds
    return capture_rate, loss_rate


def _series_indexes(count: int) -> np.ndarray:
    if count <= MAX_SERIES_POINTS:
        return np.arange(count, dtype=np.int64)
    return np.unique(np.linspace(0, count - 1, MAX_SERIES_POINTS, dtype=np.int64))


def _source_graph(run_id: uuid.UUID, entry: Any) -> CaptureCadenceGraph:
    resources = dict(entry.list_resources())
    session_id, expected_rate_hz, session_payload = _capture_session(entry, resources)
    source_digests = {CAPTURE_SESSION_RESOURCE: _resource_digest(session_payload)}
    captures: list[_RawCapture] = []
    for name, resource_type in sorted(resources.items()):
        if resource_type != HDF5_SNAPSHOT_RESOURCE_TYPE or not name.endswith(".h5"):
            continue
        snapshot_id = _snapshot_id_from_name(name)
        payload = _resource_bytes(entry, name, resource_type)
        source_digests[name] = _resource_digest(payload)
        captures.extend(_native_snapshot_captures(payload, snapshot_id))
    if not captures:
        raise CaptureCadenceGraphNotReady("Exposure has no native HDF5 capture data.")
    captures.sort(key=lambda item: item.sequence)
    if any(capture.session_id != session_id for capture in captures):
        raise CaptureCadenceGraphValidationError("Snapshot session does not match capture provenance.")
    if any(right.sequence <= left.sequence for left, right in zip(captures, captures[1:])):
        raise CaptureCadenceGraphValidationError("Capture sequences must increase across snapshots.")
    if any(
        right.captured_at_monotonic_ns <= left.captured_at_monotonic_ns
        for left, right in zip(captures, captures[1:])
    ):
        raise CaptureCadenceGraphValidationError("Capture monotonic timestamps must increase across snapshots.")

    events: tuple[Any, ...] = ()
    issues: list[str] = []
    if RUN_EVENT_RESOURCE in resources:
        payload = _resource_bytes(entry, RUN_EVENT_RESOURCE, resources[RUN_EVENT_RESOURCE])
        source_digests[RUN_EVENT_RESOURCE] = _resource_digest(payload)
        try:
            timeline = load_run_event_timeline(entry)
        except ValueError as exc:
            raise CaptureCadenceGraphValidationError(f"Run event timeline is invalid: {exc}") from exc
        events = tuple(
            event
            for event in timeline.events
            if event.capture_session_id in {None, session_id}
        )
        issues.extend(issue.message for issue in timeline.issues)
    if "end_metadata.json" in resources:
        payload = _resource_bytes(entry, "end_metadata.json", resources["end_metadata.json"])
        source_digests["end_metadata.json"] = _resource_digest(payload)

    segments, segment_issues = _trigger_segments(captures, events)
    issues.extend(segment_issues)
    gaps = []
    gap_capture_indexes = []
    gaps_by_capture_index = np.zeros(len(captures), dtype=np.int64)
    for index in range(1, len(captures)):
        if segments[index] != segments[index - 1]:
            continue
        previous = captures[index - 1]
        current = captures[index]
        gap = infer_gap(
            PulseCadenceObservation(
                previous.session_id,
                previous.sequence,
                previous.captured_at_unix_ns,
                previous.captured_at_monotonic_ns,
            ),
            PulseCadenceObservation(
                current.session_id,
                current.sequence,
                current.captured_at_unix_ns,
                current.captured_at_monotonic_ns,
            ),
            expected_rate_hz,
        )
        if gap.estimated_lost_count <= 0:
            continue
        gaps.append(gap)
        gap_capture_indexes.append(index)
        gaps_by_capture_index[index] = gap.estimated_lost_count

    capture_rate, loss_rate = _rolling_series(captures, segments, gaps_by_capture_index)
    elapsed = np.asarray(
        [
            (capture.captured_at_monotonic_ns - captures[0].captured_at_monotonic_ns) / 1e9
            for capture in captures
        ],
        dtype=np.float64,
    )
    selected = _series_indexes(len(captures))
    gap_indexes = np.asarray(gap_capture_indexes, dtype=np.int64)
    fingerprint_payload = {
        "algorithm_version": CAPTURE_CADENCE_GRAPH_ALGORITHM_VERSION,
        "run_id": str(run_id),
        "expected_rate_hz": expected_rate_hz,
        "resources": dict(sorted(source_digests.items())),
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return CaptureCadenceGraph(
        run_id=run_id,
        session_id=session_id,
        generated_at_unix_seconds=time.time(),
        source_fingerprint=fingerprint,
        expected_rate_hz=expected_rate_hz,
        quality=CadenceQuality.TIMESTAMP_INFERRED,
        raw_capture_count=len(captures),
        inferred_lost_count=sum(gap.estimated_lost_count for gap in gaps),
        ambiguous_gap_count=sum(gap.confidence is GapConfidence.LOW for gap in gaps),
        issues=tuple(issues),
        elapsed_seconds=elapsed[selected],
        source_sequence=np.asarray([capture.sequence for capture in captures], dtype=np.int64)[selected],
        segment_id=segments[selected],
        rolling_window_seconds=np.asarray(ROLLING_WINDOW_OPTIONS_SECONDS, dtype=np.float64),
        capture_rate_hz=capture_rate[:, selected],
        estimated_lost_per_second=loss_rate[:, selected],
        gap_elapsed_seconds=elapsed[gap_indexes],
        gap_sequence_before=np.asarray([gap.sequence_before for gap in gaps], dtype=np.int64),
        gap_sequence_after=np.asarray([gap.sequence_after for gap in gaps], dtype=np.int64),
        gap_interval_seconds=np.asarray([gap.interval_seconds for gap in gaps], dtype=np.float64),
        gap_estimated_lost_count=np.asarray([gap.estimated_lost_count for gap in gaps], dtype=np.int64),
        gap_residual_seconds=np.asarray([gap.residual_seconds for gap in gaps], dtype=np.float64),
        gap_confidence_high=np.asarray([gap.confidence is GapConfidence.HIGH for gap in gaps], dtype=np.bool_),
        gap_crosses_snapshot_boundary=np.asarray(
            [captures[index - 1].snapshot_id != captures[index].snapshot_id for index in gap_capture_indexes],
            dtype=np.bool_,
        ),
    )


def _validate_graph(graph: CaptureCadenceGraph) -> None:
    point_count = len(graph.elapsed_seconds)
    gap_count = len(graph.gap_elapsed_seconds)
    if graph.quality is not CadenceQuality.TIMESTAMP_INFERRED:
        raise CaptureCadenceGraphValidationError("Persisted cadence quality is unsupported.")
    if not math.isfinite(graph.expected_rate_hz) or graph.expected_rate_hz <= 0:
        raise CaptureCadenceGraphValidationError("Persisted expected cadence is invalid.")
    if graph.raw_capture_count < 1 or point_count < 1 or point_count > MAX_SERIES_POINTS:
        raise CaptureCadenceGraphValidationError("Persisted cadence point counts are invalid.")
    if graph.raw_capture_count < point_count:
        raise CaptureCadenceGraphValidationError("Persisted cadence contains too many series points.")
    if graph.rolling_window_seconds.shape != (len(ROLLING_WINDOW_OPTIONS_SECONDS),) or not np.array_equal(
        graph.rolling_window_seconds,
        np.asarray(ROLLING_WINDOW_OPTIONS_SECONDS, dtype=np.float64),
    ):
        raise CaptureCadenceGraphValidationError("Persisted cadence rolling windows are unsupported.")
    point_arrays = (graph.elapsed_seconds, graph.source_sequence, graph.segment_id)
    if any(array.ndim != 1 or len(array) != point_count for array in point_arrays):
        raise CaptureCadenceGraphValidationError("Persisted cadence point arrays are inconsistent.")
    if graph.capture_rate_hz.shape != (len(ROLLING_WINDOW_OPTIONS_SECONDS), point_count) or (
        graph.estimated_lost_per_second.shape != graph.capture_rate_hz.shape
    ):
        raise CaptureCadenceGraphValidationError("Persisted cadence rolling arrays are inconsistent.")
    if not np.isfinite(graph.elapsed_seconds).all() or not np.isfinite(graph.capture_rate_hz).all() or (
        not np.isfinite(graph.estimated_lost_per_second).all()
    ):
        raise CaptureCadenceGraphValidationError("Persisted cadence series contains non-finite values.")
    if np.any(graph.elapsed_seconds < 0) or np.any(np.diff(graph.elapsed_seconds) <= 0):
        if point_count != 1 or graph.elapsed_seconds[0] != 0:
            raise CaptureCadenceGraphValidationError("Persisted cadence elapsed time must increase.")
    if np.any(graph.capture_rate_hz < 0) or np.any(graph.estimated_lost_per_second < 0):
        raise CaptureCadenceGraphValidationError("Persisted cadence rolling values must be non-negative.")
    gap_arrays = (
        graph.gap_elapsed_seconds,
        graph.gap_sequence_before,
        graph.gap_sequence_after,
        graph.gap_interval_seconds,
        graph.gap_estimated_lost_count,
        graph.gap_residual_seconds,
        graph.gap_confidence_high,
        graph.gap_crosses_snapshot_boundary,
    )
    if any(array.ndim != 1 or len(array) != gap_count for array in gap_arrays):
        raise CaptureCadenceGraphValidationError("Persisted cadence gap arrays are inconsistent.")
    if gap_count and (
        not np.isfinite(graph.gap_elapsed_seconds).all()
        or not np.isfinite(graph.gap_interval_seconds).all()
        or not np.isfinite(graph.gap_residual_seconds).all()
        or np.any(graph.gap_estimated_lost_count <= 0)
        or np.any(graph.gap_sequence_after <= graph.gap_sequence_before)
    ):
        raise CaptureCadenceGraphValidationError("Persisted cadence gap values are invalid.")
    if int(graph.gap_estimated_lost_count.sum()) != graph.inferred_lost_count:
        raise CaptureCadenceGraphValidationError("Persisted cadence loss total is inconsistent.")
    if int(np.count_nonzero(~graph.gap_confidence_high)) != graph.ambiguous_gap_count:
        raise CaptureCadenceGraphValidationError("Persisted cadence confidence total is inconsistent.")


def _write_capture_cadence_graph(path: Path, graph: CaptureCadenceGraph) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with h5py.File(temporary, "w") as destination:
            destination.attrs["schema_version"] = CAPTURE_CADENCE_GRAPH_SCHEMA_VERSION
            destination.attrs["run_id"] = str(graph.run_id)
            destination.attrs["session_id"] = str(graph.session_id)
            destination.attrs["generated_at_unix_seconds"] = graph.generated_at_unix_seconds
            destination.attrs["algorithm_version"] = CAPTURE_CADENCE_GRAPH_ALGORITHM_VERSION
            destination.attrs["source_fingerprint"] = graph.source_fingerprint
            destination.attrs["expected_rate_hz"] = graph.expected_rate_hz
            destination.attrs["quality"] = graph.quality.value
            destination.attrs["raw_capture_count"] = graph.raw_capture_count
            destination.attrs["series_point_count"] = len(graph.elapsed_seconds)
            destination.attrs["inferred_lost_count"] = graph.inferred_lost_count
            destination.attrs["ambiguous_gap_count"] = graph.ambiguous_gap_count
            destination.attrs["issues_json"] = json.dumps(graph.issues, allow_nan=False, separators=(",", ":"))
            for name in _DATASETS:
                destination.create_dataset(name, data=getattr(graph, name), compression="gzip", shuffle=True)
            destination.flush()
        with temporary.open("r+b") as output:
            os.fsync(output.fileno())
        read_capture_cadence_graph_path(temporary, expected_run_id=graph.run_id)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_capture_cadence_graph_path(
    path: str | Path,
    *,
    expected_run_id: uuid.UUID | None = None,
) -> CaptureCadenceGraph:
    try:
        with h5py.File(Path(path), "r") as source:
            if set(source.keys()) != _DATASETS or set(source.attrs) != _REQUIRED_ATTRIBUTES:
                raise CaptureCadenceGraphValidationError("Cadence graph has unknown or missing fields.")
            if int(source.attrs["schema_version"]) != CAPTURE_CADENCE_GRAPH_SCHEMA_VERSION:
                raise CaptureCadenceGraphValidationError("Unsupported cadence graph schema version.")
            if _decode_attribute(source.attrs["algorithm_version"]) != CAPTURE_CADENCE_GRAPH_ALGORITHM_VERSION:
                raise CaptureCadenceGraphValidationError("Unsupported cadence graph algorithm version.")
            run_id = uuid.UUID(_decode_attribute(source.attrs["run_id"]))
            if expected_run_id is not None and run_id != expected_run_id:
                raise CaptureCadenceGraphValidationError("Cadence graph run ID does not match its requested run.")
            issues = json.loads(_decode_attribute(source.attrs["issues_json"]))
            if not isinstance(issues, list) or not all(isinstance(item, str) for item in issues):
                raise CaptureCadenceGraphValidationError("Cadence graph issues attribute is invalid.")
            values = {name: np.asarray(source[name][:]) for name in _DATASETS}
            graph = CaptureCadenceGraph(
                run_id=run_id,
                session_id=uuid.UUID(_decode_attribute(source.attrs["session_id"])),
                generated_at_unix_seconds=float(source.attrs["generated_at_unix_seconds"]),
                source_fingerprint=_decode_attribute(source.attrs["source_fingerprint"]),
                expected_rate_hz=float(source.attrs["expected_rate_hz"]),
                quality=CadenceQuality(_decode_attribute(source.attrs["quality"])),
                raw_capture_count=int(source.attrs["raw_capture_count"]),
                inferred_lost_count=int(source.attrs["inferred_lost_count"]),
                ambiguous_gap_count=int(source.attrs["ambiguous_gap_count"]),
                issues=tuple(issues),
                **values,
            )
            if int(source.attrs["series_point_count"]) != len(graph.elapsed_seconds):
                raise CaptureCadenceGraphValidationError("Cadence graph point-count attribute is inconsistent.")
    except (OSError, ValueError, KeyError) as exc:
        if isinstance(exc, CaptureCadenceGraphValidationError):
            raise
        raise CaptureCadenceGraphValidationError(f"Cadence graph is invalid or unavailable: {exc}") from exc
    _validate_graph(graph)
    return graph


def read_capture_cadence_graph(entry: Any, data_path: str | Path, run_id: uuid.UUID) -> CaptureCadenceGraph:
    resources = dict(entry.list_resources())
    if resources.get(CAPTURE_CADENCE_GRAPH_RESOURCE) != CAPTURE_CADENCE_GRAPH_RESOURCE_TYPE:
        raise FileNotFoundError("Capture cadence graph is not registered.")
    return read_capture_cadence_graph_path(
        _entry_folder(data_path, entry) / CAPTURE_CADENCE_GRAPH_RESOURCE,
        expected_run_id=run_id,
    )


def ensure_capture_cadence_graph(
    run_id: uuid.UUID,
    entry: Any,
    data_path: str | Path,
    *,
    allow_incomplete: bool = False,
) -> EnsureCaptureCadenceGraphResult:
    if not isinstance(run_id, uuid.UUID):
        raise ValueError("Capture cadence graph run ID must be a UUID.")
    if not allow_incomplete and dict(entry.list_resources()).get("end_metadata.json") != "metadata":
        return EnsureCaptureCadenceGraphResult("waiting_for_completion", None)
    folder = _entry_folder(data_path, entry)
    lock_path = Path(data_path).resolve() / ".locks" / "capture-cadence" / f"{run_id}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with portalocker.Lock(
            lock_path,
            mode="a+b",
            timeout=0,
            flags=portalocker.LockFlags.EXCLUSIVE | portalocker.LockFlags.NON_BLOCKING,
        ):
            graph = _source_graph(run_id, entry)
            path = folder / CAPTURE_CADENCE_GRAPH_RESOURCE
            previous = None
            try:
                previous = read_capture_cadence_graph_path(path, expected_run_id=run_id)
            except (FileNotFoundError, CaptureCadenceGraphValidationError):
                pass
            if previous is not None and previous.source_fingerprint == graph.source_fingerprint:
                entry.register_existing_resource(CAPTURE_CADENCE_GRAPH_RESOURCE, CAPTURE_CADENCE_GRAPH_RESOURCE_TYPE)
                return EnsureCaptureCadenceGraphResult("existing", previous)
            _write_capture_cadence_graph(path, graph)
            entry.register_existing_resource(CAPTURE_CADENCE_GRAPH_RESOURCE, CAPTURE_CADENCE_GRAPH_RESOURCE_TYPE)
            return EnsureCaptureCadenceGraphResult("replaced" if previous is not None else "generated", graph)
    except portalocker.exceptions.LockException:
        return EnsureCaptureCadenceGraphResult("busy", None)