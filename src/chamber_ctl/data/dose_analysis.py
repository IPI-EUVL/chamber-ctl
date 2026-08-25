from __future__ import annotations

import hashlib
import io
import json
import math
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np

from chamber_ctl.data.calibration import CalibrationProfile


ANALYSIS_SCHEMA_VERSION = 1
ANALYSIS_RESOURCE_TYPE = "dose_analysis"
HDF5_SNAPSHOT_RESOURCE_TYPE = "euv_snapshot"
LEGACY_ANALYSIS_VERSION = "legacy-siglent-v1-sequence-gap-compensation"
HDF5_ANALYSIS_VERSION = "pitaya-hdf5-v1-exact-pulse-sum"
CALIBRATION_PROVENANCE_RESOURCE = "euv_calibration_profile.json"
CAPTURE_TIMELINE_RESOURCE = "euv_capture_timeline.json"
CAPTURE_TIMELINE_RESOURCE_TYPE = "euv_capture_timeline"
CAPTURE_TIMELINE_SCHEMA_VERSION = 1


def load_hdf5_snapshot_pulses(entry, snapshot_id: uuid.UUID) -> np.ndarray:
    """Return an HDF5 snapshot as pulses shaped ``(pulse, sample, time/voltage)``."""
    import h5py

    filename = f"snap_{snapshot_id}.h5"
    with entry.resource(filename, HDF5_SNAPSHOT_RESOURCE_TYPE, "rb") as resource:
        payload = resource.read()
    with h5py.File(io.BytesIO(payload), "r") as snapshot:
        samples = snapshot["samples_v"][:]
        sample_rate_hz = float(snapshot.attrs["sample_rate_hz"])
        pretrigger_seconds = float(snapshot.attrs["pretrigger_seconds"])
    if samples.ndim != 2 or samples.dtype != np.dtype("float32"):
        raise ValueError("HDF5 snapshot samples_v must be a two-dimensional float32 dataset.")
    if sample_rate_hz <= 0 or pretrigger_seconds <= 0:
        raise ValueError("HDF5 snapshot timing attributes are invalid.")
    time_axis = np.arange(samples.shape[1], dtype=np.float64) / sample_rate_hz - pretrigger_seconds
    pulses = np.empty((samples.shape[0], samples.shape[1], 2), dtype=np.float64)
    pulses[:, :, 0] = time_axis
    pulses[:, :, 1] = samples
    return pulses


@dataclass(frozen=True)
class CaptureTimelinePoint:
    snapshot_id: uuid.UUID
    final_sequence: int
    cumulative_dose_mj_cm2: float
    cumulative_runtime_seconds: float

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot_id, uuid.UUID):
            raise ValueError("Capture timeline snapshot ID must be a UUID.")
        if isinstance(self.final_sequence, bool) or not isinstance(self.final_sequence, int) or self.final_sequence < 0:
            raise ValueError("Capture timeline final sequence must be a non-negative integer.")
        if self.cumulative_dose_mj_cm2 < 0 or not math.isfinite(self.cumulative_dose_mj_cm2):
            raise ValueError("Capture timeline cumulative dose must be finite and non-negative.")
        if self.cumulative_runtime_seconds < 0 or not math.isfinite(self.cumulative_runtime_seconds):
            raise ValueError("Capture timeline cumulative runtime must be finite and non-negative.")

    def to_dict(self) -> dict:
        return {
            "snapshot_id": str(self.snapshot_id),
            "final_sequence": self.final_sequence,
            "cumulative_dose_mj_cm2": self.cumulative_dose_mj_cm2,
            "cumulative_runtime_seconds": self.cumulative_runtime_seconds,
        }

    @classmethod
    def from_dict(cls, value: object) -> "CaptureTimelinePoint":
        expected = {
            "snapshot_id",
            "final_sequence",
            "cumulative_dose_mj_cm2",
            "cumulative_runtime_seconds",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("Capture timeline point contains unknown or missing fields.")
        return cls(
            snapshot_id=uuid.UUID(str(value["snapshot_id"])),
            final_sequence=int(value["final_sequence"]),
            cumulative_dose_mj_cm2=float(value["cumulative_dose_mj_cm2"]),
            cumulative_runtime_seconds=float(value["cumulative_runtime_seconds"]),
        )


def load_capture_timeline(entry) -> tuple[CaptureTimelinePoint, ...]:
    resources = dict(entry.list_resources())
    if CAPTURE_TIMELINE_RESOURCE not in resources:
        return ()
    if resources[CAPTURE_TIMELINE_RESOURCE] != CAPTURE_TIMELINE_RESOURCE_TYPE:
        raise ValueError("Capture timeline resource has an unexpected type.")
    with entry.resource(CAPTURE_TIMELINE_RESOURCE, CAPTURE_TIMELINE_RESOURCE_TYPE, "r") as resource:
        value = json.load(resource)
    if not isinstance(value, dict) or set(value) != {"schema_version", "snapshots"}:
        raise ValueError("Capture timeline contains unknown or missing fields.")
    if value["schema_version"] != CAPTURE_TIMELINE_SCHEMA_VERSION or not isinstance(value["snapshots"], list):
        raise ValueError("Capture timeline schema is invalid.")
    points = tuple(CaptureTimelinePoint.from_dict(item) for item in value["snapshots"])
    if tuple(sorted(points, key=lambda item: item.final_sequence)) != points:
        raise ValueError("Capture timeline snapshots must be ordered by final sequence.")
    if len({item.snapshot_id for item in points}) != len(points) or len({item.final_sequence for item in points}) != len(points):
        raise ValueError("Capture timeline contains duplicate snapshots or sequences.")
    if any(
        later.cumulative_dose_mj_cm2 < earlier.cumulative_dose_mj_cm2
        or later.cumulative_runtime_seconds < earlier.cumulative_runtime_seconds
        for earlier, later in zip(points, points[1:])
    ):
        raise ValueError("Capture timeline cumulative values must not decrease.")
    return points


def append_capture_timeline_point(entry, point: CaptureTimelinePoint) -> None:
    points = list(load_capture_timeline(entry))
    existing = next((item for item in points if item.snapshot_id == point.snapshot_id), None)
    if existing is not None:
        if existing != point:
            raise ValueError("Capture timeline snapshot conflicts with its existing entry.")
        return
    if any(item.final_sequence == point.final_sequence for item in points):
        raise ValueError("Capture timeline final sequence already belongs to another snapshot.")
    points.append(point)
    points.sort(key=lambda item: item.final_sequence)
    for earlier, later in zip(points, points[1:]):
        if later.cumulative_dose_mj_cm2 < earlier.cumulative_dose_mj_cm2:
            raise ValueError(
                "Capture timeline cumulative dose must not decrease "
                f"({earlier.cumulative_dose_mj_cm2} to {later.cumulative_dose_mj_cm2})."
            )
        if later.cumulative_runtime_seconds < earlier.cumulative_runtime_seconds:
            raise ValueError(
                "Capture timeline cumulative runtime must not decrease "
                f"({earlier.cumulative_runtime_seconds} to {later.cumulative_runtime_seconds})."
            )
    with entry.resource(CAPTURE_TIMELINE_RESOURCE, CAPTURE_TIMELINE_RESOURCE_TYPE, "w") as resource:
        json.dump(
            {
                "schema_version": CAPTURE_TIMELINE_SCHEMA_VERSION,
                "snapshots": [item.to_dict() for item in points],
            },
            resource,
            allow_nan=False,
            separators=(",", ":"),
        )


def legacy_default_profile() -> CalibrationProfile:
    return CalibrationProfile(
        profile_id=uuid.uuid5(uuid.NAMESPACE_URL, "euvl/legacy-siglent-dose-v1"),
        revision=1,
        name="Legacy Siglent Dose Calibration",
        created_at=0.0,
        algorithm_version=LEGACY_ANALYSIS_VERSION,
        signal_polarity=1,
        load_resistance_ohms=50.0,
        photodiode_responsivity_a_per_w=0.14,
        illuminated_area_cm2=0.05,
        provenance="Migrated from historical oscilloscope dose constants.",
        notes="Reproduces legacy sequence-mode gap compensation.",
    )


def load_experiment_calibration(entry) -> CalibrationProfile:
    resources = dict(entry.list_resources())
    if CALIBRATION_PROVENANCE_RESOURCE not in resources:
        return legacy_default_profile()
    if resources[CALIBRATION_PROVENANCE_RESOURCE] != "euv_calibration_profile":
        raise ValueError("Experiment calibration resource has an unexpected type.")
    with entry.resource(CALIBRATION_PROVENANCE_RESOURCE, "euv_calibration_profile", "r") as resource:
        return CalibrationProfile.from_dict(json.load(resource))


def _sha256_path(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _finite(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"{name} must be finite.")
    return float(value)


@dataclass(frozen=True)
class SnapshotDoseSummary:
    snapshot_id: uuid.UUID
    source_format: str
    source_algorithm_version: str
    source_sha256: str
    pulse_count: int
    first_sequence: int | None
    final_sequence: int | None
    total_dose_mj_cm2: float
    average_pulse_dose_mj_cm2: float
    runtime_seconds: float = 0.0

    def to_dict(self) -> dict:
        return {
            "snapshot_id": str(self.snapshot_id),
            "source_format": self.source_format,
            "source_algorithm_version": self.source_algorithm_version,
            "source_sha256": self.source_sha256,
            "pulse_count": self.pulse_count,
            "first_sequence": self.first_sequence,
            "final_sequence": self.final_sequence,
            "total_dose_mj_cm2": self.total_dose_mj_cm2,
            "average_pulse_dose_mj_cm2": self.average_pulse_dose_mj_cm2,
            "runtime_seconds": self.runtime_seconds,
        }


@dataclass(frozen=True)
class DoseAnalysisResult:
    run_uuid: uuid.UUID
    calibration: CalibrationProfile
    snapshots: tuple[SnapshotDoseSummary, ...]
    runtime_seconds: float
    live_total_dose_mj_cm2: float | None = None

    @property
    def total_dose_mj_cm2(self) -> float:
        return float(sum(item.total_dose_mj_cm2 for item in self.snapshots))

    @property
    def average_dose_rate_mj_cm2_s(self) -> float | None:
        if self.runtime_seconds <= 0:
            return None
        return self.total_dose_mj_cm2 / self.runtime_seconds


@dataclass(frozen=True)
class DoseAnalysisRevision:
    analysis_id: uuid.UUID
    created_at: float
    result: DoseAnalysisResult

    def to_dict(self) -> dict:
        return {
            "schema_version": ANALYSIS_SCHEMA_VERSION,
            "analysis_id": str(self.analysis_id),
            "created_at": self.created_at,
            "run_uuid": str(self.result.run_uuid),
            "calibration": self.result.calibration.to_dict(),
            "snapshots": [snapshot.to_dict() for snapshot in self.result.snapshots],
            "runtime_seconds": self.result.runtime_seconds,
            "total_dose_mj_cm2": self.result.total_dose_mj_cm2,
            "average_dose_rate_mj_cm2_s": self.result.average_dose_rate_mj_cm2_s,
            "live_total_dose_mj_cm2": self.result.live_total_dose_mj_cm2,
        }


@dataclass(frozen=True)
class DoseSeries:
    snapshot_ids: tuple[uuid.UUID, ...]
    cumulative_runtime_seconds: np.ndarray
    cumulative_dose_mj_cm2: np.ndarray
    has_exact_runtime: bool


@dataclass(frozen=True)
class PeakVoltageSeries:
    time_seconds: np.ndarray
    peak_volts: np.ndarray


def analyze_hdf5_snapshot(path: str | Path, calibration: CalibrationProfile) -> SnapshotDoseSummary:
    from euv_acquisition.analysis import analyze_pulse
    from euv_acquisition.snapshot import read_snapshot

    source_path = Path(path)
    contents = read_snapshot(source_path)
    doses = [
        max(0.0, calibration.dose_for_integral(analyze_pulse(samples, contents.capture_config).integral_volt_seconds))
        for samples in contents.samples_v
    ]
    total = float(sum(doses))
    return SnapshotDoseSummary(
        snapshot_id=contents.snapshot_id,
        source_format="pitaya_hdf5",
        source_algorithm_version=HDF5_ANALYSIS_VERSION,
        source_sha256=_sha256_path(source_path),
        pulse_count=len(doses),
        first_sequence=int(contents.sequence[0]),
        final_sequence=int(contents.sequence[-1]),
        total_dose_mj_cm2=total,
        average_pulse_dose_mj_cm2=total / len(doses),
    )


def analyze_hdf5_snapshot_resource(entry, snapshot_id: uuid.UUID, calibration: CalibrationProfile) -> SnapshotDoseSummary:
    import h5py
    from euv_acquisition.analysis import analyze_pulse
    from euv_acquisition.models import CaptureConfig

    filename = f"snap_{snapshot_id}.h5"
    with entry.resource(filename, HDF5_SNAPSHOT_RESOURCE_TYPE, "rb") as resource:
        payload = resource.read()
    with h5py.File(io.BytesIO(payload), "r") as snapshot:
        samples = snapshot["samples_v"][:]
        sequence = snapshot["sequence"][:]
        config = CaptureConfig(
            sample_rate_hz=float(snapshot.attrs["sample_rate_hz"]),
            window_seconds=float(snapshot.attrs["window_seconds"]),
            pretrigger_seconds=float(snapshot.attrs["pretrigger_seconds"]),
            input_full_scale_volts=float(snapshot.attrs["input_full_scale_volts"]),
            clipping_fraction=float(snapshot.attrs["clipping_fraction"]),
        )
        file_snapshot_id = uuid.UUID(str(snapshot.attrs["snapshot_id"]))
    if file_snapshot_id != snapshot_id:
        raise ValueError("HDF5 snapshot resource ID does not match its filename.")
    if samples.ndim != 2 or len(sequence) != len(samples):
        raise ValueError("HDF5 snapshot pulse datasets have invalid shapes.")
    if len(sequence) == 0 or not np.array_equal(sequence, np.arange(sequence[0], sequence[0] + len(sequence))):
        raise ValueError("HDF5 snapshot sequences must be contiguous and ordered.")
    doses = [
        max(0.0, calibration.dose_for_integral(analyze_pulse(pulse, config).integral_volt_seconds))
        for pulse in samples
    ]
    total = float(sum(doses))
    return SnapshotDoseSummary(
        snapshot_id=snapshot_id,
        source_format="pitaya_hdf5",
        source_algorithm_version=HDF5_ANALYSIS_VERSION,
        source_sha256=hashlib.sha256(payload).hexdigest(),
        pulse_count=len(doses),
        first_sequence=int(sequence[0]),
        final_sequence=int(sequence[-1]),
        total_dose_mj_cm2=total,
        average_pulse_dose_mj_cm2=total / len(doses),
    )


def analyze_legacy_snapshot(
    snapshot_id: uuid.UUID,
    start_unix_ns: int | float,
    end_unix_ns: int | float,
    waveform: np.ndarray,
    indexes: np.ndarray,
    metadata: dict[str, Any],
    calibration: CalibrationProfile,
    *,
    source_sha256: str,
) -> SnapshotDoseSummary:
    data = np.asarray(waveform, dtype=float)
    pulse_indexes = np.asarray(indexes)
    if data.ndim != 2 or data.shape[1] < 2 or not np.isfinite(data[:, :2]).all():
        raise ValueError("Legacy snapshot waveform must have finite time and voltage columns.")
    if pulse_indexes.ndim != 2 or pulse_indexes.shape[1] < 2:
        raise ValueError("Legacy snapshot indexes must include sample index and timestamp columns.")
    if len(pulse_indexes) == 0:
        return SnapshotDoseSummary(snapshot_id, "legacy_siglent_npz", LEGACY_ANALYSIS_VERSION, source_sha256, 0, None, None, 0.0, 0.0)
    sample_indexes = pulse_indexes[:, 0].astype(int)
    pulse_times = pulse_indexes[:, 1].astype(float)
    if np.any(sample_indexes < 0) or np.any(sample_indexes >= len(data)) or np.any(np.diff(sample_indexes) <= 0):
        raise ValueError("Legacy snapshot indexes must be ordered valid sample offsets.")
    if not np.isfinite(pulse_times).all() or np.any(np.diff(pulse_times) < 0):
        raise ValueError("Legacy snapshot pulse timestamps must be finite and ordered.")

    pulse_doses = []
    for position, sample_index in enumerate(sample_indexes):
        stop = sample_indexes[position + 1] if position + 1 < len(sample_indexes) else len(data)
        pulse = data[sample_index:stop, :2]
        baseline = float(np.mean(pulse[: min(25, len(pulse)), 1]))
        integral = float(np.trapezoid(pulse[:, 1] - baseline, pulse[:, 0]))
        pulse_doses.append(calibration.dose_for_integral(integral))

    average = float(np.mean(pulse_doses))
    pulse_span = float(pulse_times[-1] - pulse_times[0]) if len(pulse_times) > 1 else 0.0
    inferred_step = len(pulse_doses) < 2 or float(np.mean(pulse_doses[-50:])) < 0.1 * average
    is_step = metadata.get("is_step_exposure", inferred_step)
    if not isinstance(is_step, bool):
        raise ValueError("Legacy is_step_exposure must be boolean.")
    wall_duration = (_finite("end_unix_ns", end_unix_ns) - _finite("start_unix_ns", start_unix_ns)) / 1e9
    if wall_duration < 0:
        raise ValueError("Legacy snapshot end timestamp precedes start timestamp.")
    effective_duration = pulse_span if is_step else wall_duration
    total = average * effective_duration * 100.0

    if "exposure_start_ns" not in metadata:
        runtime = effective_duration
    elif metadata["exposure_start_ns"] is None:
        runtime = 0.0
    else:
        runtime = max(
            0.0,
            _finite("end_unix_ns", end_unix_ns)
            - max(_finite("start_unix_ns", start_unix_ns), _finite("exposure_start_ns", metadata["exposure_start_ns"]))
        ) / 1e9
    return SnapshotDoseSummary(
        snapshot_id=snapshot_id,
        source_format="legacy_siglent_npz",
        source_algorithm_version=LEGACY_ANALYSIS_VERSION,
        source_sha256=source_sha256,
        pulse_count=len(pulse_doses),
        first_sequence=None,
        final_sequence=None,
        total_dose_mj_cm2=total,
        average_pulse_dose_mj_cm2=average,
        runtime_seconds=runtime,
    )


def _snapshot_capture_start_ns(entry, snapshot_id: uuid.UUID, resource_type: str) -> int:
    filename = f"snap_{snapshot_id}.h5" if resource_type == HDF5_SNAPSHOT_RESOURCE_TYPE else f"snap_{snapshot_id}.json"
    if resource_type == HDF5_SNAPSHOT_RESOURCE_TYPE:
        import h5py

        with entry.resource(filename, resource_type, "rb") as resource:
            payload = resource.read()
        with h5py.File(io.BytesIO(payload), "r") as snapshot:
            captured_at = snapshot["captured_at_unix_ns"][:]
        if captured_at.ndim != 1 or len(captured_at) == 0:
            raise ValueError("HDF5 snapshot capture timestamps are invalid.")
        return int(captured_at[0])

    with entry.resource(filename, "snap_meta", "r") as resource:
        metadata = json.load(resource)
    return int(_finite("legacy snapshot start", metadata["start"]))


def _ordered_snapshot_summaries(entry, result: DoseAnalysisResult) -> list[tuple[SnapshotDoseSummary, int]]:
    resources = dict(entry.list_resources())
    ordered = []
    for summary in result.snapshots:
        filename = f"snap_{summary.snapshot_id}.h5" if summary.source_format == "pitaya_hdf5" else f"snap_{summary.snapshot_id}.npz"
        resource_type = resources.get(filename)
        if resource_type not in (HDF5_SNAPSHOT_RESOURCE_TYPE, "snapshot"):
            raise ValueError(f"Snapshot {summary.snapshot_id} is missing from its experiment entry.")
        ordered.append((summary, _snapshot_capture_start_ns(entry, summary.snapshot_id, resource_type)))
    return sorted(ordered, key=lambda item: (item[1], str(item[0].snapshot_id)))


def load_experiment_dose_series(run_uuid: uuid.UUID, entry) -> DoseSeries:
    result = analyze_experiment_entry(run_uuid, entry)
    ordered = _ordered_snapshot_summaries(entry, result)
    timeline = {point.snapshot_id: point for point in load_capture_timeline(entry)}
    cumulative_dose = []
    cumulative_runtime = []
    running_dose = 0.0
    running_runtime = 0.0
    missing_hdf5_timeline = False

    for summary, _capture_start_ns in ordered:
        running_dose += summary.total_dose_mj_cm2
        if summary.source_format == "pitaya_hdf5":
            point = timeline.get(summary.snapshot_id)
            if point is None:
                missing_hdf5_timeline = True
            else:
                running_runtime = point.cumulative_runtime_seconds
        else:
            running_runtime += summary.runtime_seconds
        cumulative_dose.append(running_dose)
        cumulative_runtime.append(running_runtime)

    if missing_hdf5_timeline and cumulative_runtime:
        cumulative_runtime[-1] = max(cumulative_runtime[-1], result.runtime_seconds)

    return DoseSeries(
        snapshot_ids=tuple(summary.snapshot_id for summary, _capture_start_ns in ordered),
        cumulative_runtime_seconds=np.asarray(cumulative_runtime, dtype=float),
        cumulative_dose_mj_cm2=np.asarray(cumulative_dose, dtype=float),
        has_exact_runtime=not missing_hdf5_timeline,
    )


def load_experiment_peak_voltage_series(entry) -> PeakVoltageSeries:
    result = analyze_experiment_entry(uuid.uuid4(), entry)
    ordered = _ordered_snapshot_summaries(entry, result)
    timestamps = []
    peaks = []

    for summary, _capture_start_ns in ordered:
        if summary.source_format == "pitaya_hdf5":
            import h5py

            filename = f"snap_{summary.snapshot_id}.h5"
            with entry.resource(filename, HDF5_SNAPSHOT_RESOURCE_TYPE, "rb") as resource:
                payload = resource.read()
            with h5py.File(io.BytesIO(payload), "r") as snapshot:
                captured_at_unix_ns = snapshot["captured_at_unix_ns"][:]
            pulses = load_hdf5_snapshot_pulses(entry, summary.snapshot_id)
            if len(captured_at_unix_ns) != len(pulses):
                raise ValueError("HDF5 snapshot pulse timestamps do not match waveform count.")
            timestamps.extend(float(value) / 1e9 for value in captured_at_unix_ns)
            peaks.extend(float(np.max(pulse[:, 1])) for pulse in pulses)
            continue

        filename = f"snap_{summary.snapshot_id}.npz"
        with entry.resource(filename, "snapshot", "rb") as resource:
            archive = np.load(io.BytesIO(resource.read()))
            waveform = archive["data"]
            indexes = archive["indexes"]
        for position, (sample_index, pulse_time) in enumerate(indexes):
            stop = int(indexes[position + 1, 0]) if position + 1 < len(indexes) else len(waveform)
            timestamps.append(float(pulse_time))
            peaks.append(float(np.max(waveform[int(sample_index):stop, 1])))

    if not timestamps:
        return PeakVoltageSeries(np.asarray([], dtype=float), np.asarray([], dtype=float))
    order = np.argsort(np.asarray(timestamps, dtype=float))
    ordered_times = np.asarray(timestamps, dtype=float)[order]
    return PeakVoltageSeries(ordered_times - ordered_times[0], np.asarray(peaks, dtype=float)[order])


def write_analysis_revision(entry, revision: DoseAnalysisRevision, *, promote: bool) -> str:
    filename = f"dose_analysis_{revision.analysis_id}.json"
    with entry.resource(filename, ANALYSIS_RESOURCE_TYPE, "w") as resource:
        json.dump(revision.to_dict(), resource, allow_nan=False, separators=(",", ":"))
    if promote:
        entry.set_tag("dose", revision.result.total_dose_mj_cm2)
        entry.set_tag("runtime", revision.result.runtime_seconds)
        entry.set_tag("active_dose_analysis", str(revision.analysis_id))
        entry.set_tag("calibration_profile_id", str(revision.result.calibration.profile_id))
        entry.set_tag("calibration_revision", str(revision.result.calibration.revision))
        entry.set_tag("calibration_hash", revision.result.calibration.content_hash)
    return filename


def analyze_experiment_entry(run_uuid: uuid.UUID, entry, *, runtime_seconds: float | None = None) -> DoseAnalysisResult:
    resources = dict(entry.list_resources())
    calibration = load_experiment_calibration(entry)

    summaries = []
    for filename, resource_type in resources.items():
        if resource_type == HDF5_SNAPSHOT_RESOURCE_TYPE and filename.startswith("snap_") and filename.endswith(".h5"):
            summaries.append(analyze_hdf5_snapshot_resource(entry, uuid.UUID(filename[5:-3]), calibration))
        elif resource_type == "snapshot" and filename.startswith("snap_") and filename.endswith(".npz"):
            snapshot_id = uuid.UUID(filename[5:-4])
            with entry.resource(filename, "snapshot", "rb") as resource:
                snapshot_payload = resource.read()
            metadata_name = f"snap_{snapshot_id}.json"
            with entry.resource(metadata_name, "snap_meta", "r") as resource:
                metadata = json.load(resource)
            archive = np.load(io.BytesIO(snapshot_payload))
            summaries.append(
                analyze_legacy_snapshot(
                    snapshot_id,
                    metadata["start"],
                    metadata["end"],
                    archive["data"],
                    archive["indexes"],
                    metadata,
                    calibration,
                    source_sha256=hashlib.sha256(snapshot_payload).hexdigest(),
                )
            )
    timeline = load_capture_timeline(entry)
    live_total_dose = timeline[-1].cumulative_dose_mj_cm2 if timeline else None
    if timeline:
        summaries_by_id = {summary.snapshot_id: summary for summary in summaries}
        previous_runtime = 0.0
        for point in timeline:
            summary = summaries_by_id.get(point.snapshot_id)
            incremental_runtime = point.cumulative_runtime_seconds - previous_runtime
            if summary is not None and summary.source_format == "pitaya_hdf5":
                summaries_by_id[point.snapshot_id] = replace(
                    summary,
                    runtime_seconds=incremental_runtime,
                )
            previous_runtime = point.cumulative_runtime_seconds
        summaries = [summaries_by_id[summary.snapshot_id] for summary in summaries]
    resolved_runtime = runtime_seconds
    if resolved_runtime is None:
        value = entry.get_tags().get("runtime")
        try:
            resolved_runtime = float(value) if value is not None else sum(item.runtime_seconds for item in summaries)
        except (TypeError, ValueError):
            resolved_runtime = sum(item.runtime_seconds for item in summaries)
    return DoseAnalysisResult(
        run_uuid,
        calibration,
        tuple(summaries),
        float(resolved_runtime),
        live_total_dose_mj_cm2=live_total_dose,
    )