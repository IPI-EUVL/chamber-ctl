from __future__ import annotations

import io
import json
import math
import uuid
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from euv_acquisition.snapshot import SnapshotManifest, read_snapshot


PREVIEW_SCHEMA_VERSION = 1
PREVIEW_TRACE_LIMIT = 16
MAX_PREVIEW_PAYLOAD_BYTES = 2 * 1024 * 1024
MAX_PREVIEW_SAMPLES_PER_TRACE = 16_384
_ARCHIVE_FIELDS = {"metadata", "samples_v", "sequence", "captured_at_unix_ns", "quality"}
_METADATA_FIELDS = {
    "schema_version",
    "context",
    "run_id",
    "session_id",
    "snapshot_id",
    "source_kind",
    "source_id",
    "close_reason",
    "first_sequence",
    "final_sequence",
    "total_pulse_count",
    "included_pulse_count",
    "first_capture_unix_ns",
    "final_capture_unix_ns",
    "sample_rate_hz",
    "window_seconds",
    "pretrigger_seconds",
}


@dataclass(frozen=True)
class AcquisitionPreview:
    context: str
    run_id: uuid.UUID | None
    session_id: uuid.UUID
    snapshot_id: uuid.UUID
    source_kind: str
    source_id: str
    close_reason: str
    first_sequence: int
    final_sequence: int
    total_pulse_count: int
    first_capture_unix_ns: int
    final_capture_unix_ns: int
    sample_rate_hz: float
    window_seconds: float
    pretrigger_seconds: float
    samples_v: np.ndarray
    sequence: np.ndarray
    captured_at_unix_ns: np.ndarray
    quality: np.ndarray

    def __post_init__(self) -> None:
        if self.context not in {"experiment", "diagnostic"}:
            raise ValueError("Preview context must be experiment or diagnostic.")
        if self.context == "experiment" and self.run_id is None:
            raise ValueError("Experiment previews require a run ID.")
        if not isinstance(self.session_id, uuid.UUID) or not isinstance(self.snapshot_id, uuid.UUID):
            raise ValueError("Preview session and snapshot IDs must be UUIDs.")
        if self.run_id is not None and not isinstance(self.run_id, uuid.UUID):
            raise ValueError("Preview run ID must be a UUID when present.")
        if not self.source_kind.strip() or not self.source_id.strip() or not self.close_reason.strip():
            raise ValueError("Preview source and close-reason fields cannot be empty.")
        if self.first_sequence < 0 or self.final_sequence < self.first_sequence:
            raise ValueError("Preview artifact sequence range is invalid.")
        if self.total_pulse_count != self.final_sequence - self.first_sequence + 1:
            raise ValueError("Preview pulse count does not match its artifact sequence range.")
        if self.first_capture_unix_ns < 0 or self.final_capture_unix_ns < self.first_capture_unix_ns:
            raise ValueError("Preview capture timestamp range is invalid.")
        for name in ("sample_rate_hz", "window_seconds"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"Preview {name} must be finite and positive.")
        if not math.isfinite(float(self.pretrigger_seconds)) or self.pretrigger_seconds < 0:
            raise ValueError("Preview pretrigger_seconds must be finite and non-negative.")

        samples = np.asarray(self.samples_v)
        if samples.dtype != np.dtype("float32") or samples.ndim != 2:
            raise ValueError("Preview samples must be a two-dimensional float32 array.")
        pulse_count, sample_count = samples.shape
        if not 1 <= pulse_count <= PREVIEW_TRACE_LIMIT:
            raise ValueError(f"Preview must contain between 1 and {PREVIEW_TRACE_LIMIT} traces.")
        if not 1 <= sample_count <= MAX_PREVIEW_SAMPLES_PER_TRACE:
            raise ValueError("Preview sample count is outside the supported range.")
        if pulse_count > self.total_pulse_count or not np.isfinite(samples).all():
            raise ValueError("Preview samples contain invalid values or exceed the artifact pulse count.")
        expected_window = sample_count / self.sample_rate_hz
        if not math.isclose(self.window_seconds, expected_window, rel_tol=1e-9, abs_tol=1e-15):
            raise ValueError("Preview sample count does not match its capture window.")

        arrays = (
            (self.sequence, np.dtype("uint64"), "sequence"),
            (self.captured_at_unix_ns, np.dtype("int64"), "capture timestamps"),
            (self.quality, np.dtype("uint32"), "quality"),
        )
        for value, dtype, label in arrays:
            array = np.asarray(value)
            if array.dtype != dtype or array.shape != (pulse_count,):
                raise ValueError(f"Preview {label} must have shape ({pulse_count},) and dtype {dtype}.")
        if pulse_count > 1 and not np.all(np.diff(self.sequence) == 1):
            raise ValueError("Preview pulse sequences must be contiguous.")
        if int(self.sequence[0]) < self.first_sequence or int(self.sequence[-1]) > self.final_sequence:
            raise ValueError("Preview pulse sequences fall outside the artifact range.")

    @property
    def included_pulse_count(self) -> int:
        return int(self.samples_v.shape[0])

    def to_pulses(self) -> np.ndarray:
        sample_count = self.samples_v.shape[1]
        time_axis = np.arange(sample_count, dtype=np.float64) / self.sample_rate_hz - self.pretrigger_seconds
        pulses = np.empty((self.included_pulse_count, sample_count, 2), dtype=np.float64)
        pulses[:, :, 0] = time_axis
        pulses[:, :, 1] = self.samples_v
        return pulses

    def encode(self) -> bytes:
        metadata = {
            "schema_version": PREVIEW_SCHEMA_VERSION,
            "context": self.context,
            "run_id": None if self.run_id is None else str(self.run_id),
            "session_id": str(self.session_id),
            "snapshot_id": str(self.snapshot_id),
            "source_kind": self.source_kind,
            "source_id": self.source_id,
            "close_reason": self.close_reason,
            "first_sequence": self.first_sequence,
            "final_sequence": self.final_sequence,
            "total_pulse_count": self.total_pulse_count,
            "included_pulse_count": self.included_pulse_count,
            "first_capture_unix_ns": self.first_capture_unix_ns,
            "final_capture_unix_ns": self.final_capture_unix_ns,
            "sample_rate_hz": self.sample_rate_hz,
            "window_seconds": self.window_seconds,
            "pretrigger_seconds": self.pretrigger_seconds,
        }
        metadata_bytes = json.dumps(metadata, allow_nan=False, separators=(",", ":")).encode("utf-8")
        output = io.BytesIO()
        np.savez_compressed(
            output,
            metadata=np.frombuffer(metadata_bytes, dtype=np.uint8),
            samples_v=self.samples_v,
            sequence=self.sequence,
            captured_at_unix_ns=self.captured_at_unix_ns,
            quality=self.quality,
        )
        payload = output.getvalue()
        if len(payload) > MAX_PREVIEW_PAYLOAD_BYTES:
            raise ValueError("Encoded acquisition preview exceeds the DDS payload limit.")
        return payload

    @classmethod
    def decode(cls, payload: bytes) -> "AcquisitionPreview":
        if not isinstance(payload, bytes) or not payload or len(payload) > MAX_PREVIEW_PAYLOAD_BYTES:
            raise ValueError("Acquisition preview payload size is invalid.")
        try:
            with np.load(io.BytesIO(payload), allow_pickle=False) as archive:
                if set(archive.files) != _ARCHIVE_FIELDS:
                    raise ValueError("Acquisition preview archive contains unknown or missing fields.")
                metadata_array = archive["metadata"]
                if metadata_array.dtype != np.dtype("uint8") or metadata_array.ndim != 1 or len(metadata_array) > 65_536:
                    raise ValueError("Acquisition preview metadata array is invalid.")
                metadata = json.loads(metadata_array.tobytes().decode("utf-8"))
                samples_v = archive["samples_v"].copy()
                sequence = archive["sequence"].copy()
                captured_at_unix_ns = archive["captured_at_unix_ns"].copy()
                quality = archive["quality"].copy()
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"Acquisition preview payload is invalid: {exc}") from exc
        if not isinstance(metadata, dict) or set(metadata) != _METADATA_FIELDS:
            raise ValueError("Acquisition preview metadata contains unknown or missing fields.")
        if metadata["schema_version"] != PREVIEW_SCHEMA_VERSION:
            raise ValueError("Unsupported acquisition preview schema version.")
        if metadata["included_pulse_count"] != len(samples_v):
            raise ValueError("Preview metadata pulse count does not match its arrays.")
        return cls(
            context=str(metadata["context"]),
            run_id=None if metadata["run_id"] is None else uuid.UUID(str(metadata["run_id"])),
            session_id=uuid.UUID(str(metadata["session_id"])),
            snapshot_id=uuid.UUID(str(metadata["snapshot_id"])),
            source_kind=str(metadata["source_kind"]),
            source_id=str(metadata["source_id"]),
            close_reason=str(metadata["close_reason"]),
            first_sequence=int(metadata["first_sequence"]),
            final_sequence=int(metadata["final_sequence"]),
            total_pulse_count=int(metadata["total_pulse_count"]),
            first_capture_unix_ns=int(metadata["first_capture_unix_ns"]),
            final_capture_unix_ns=int(metadata["final_capture_unix_ns"]),
            sample_rate_hz=float(metadata["sample_rate_hz"]),
            window_seconds=float(metadata["window_seconds"]),
            pretrigger_seconds=float(metadata["pretrigger_seconds"]),
            samples_v=samples_v,
            sequence=sequence,
            captured_at_unix_ns=captured_at_unix_ns,
            quality=quality,
        )


def build_acquisition_preview(
    path: str | Path,
    manifest: SnapshotManifest,
    *,
    context: str,
    run_id: uuid.UUID | None = None,
    trace_limit: int = PREVIEW_TRACE_LIMIT,
) -> AcquisitionPreview:
    if isinstance(trace_limit, bool) or not isinstance(trace_limit, int) or not 1 <= trace_limit <= PREVIEW_TRACE_LIMIT:
        raise ValueError(f"trace_limit must be between 1 and {PREVIEW_TRACE_LIMIT}.")
    contents = read_snapshot(path)
    if contents.snapshot_id != manifest.snapshot_id or contents.session_id != manifest.session_id:
        raise ValueError("Snapshot contents do not match the preview manifest identity.")
    if len(contents.samples_v) != manifest.pulse_count:
        raise ValueError("Snapshot contents do not match the preview manifest pulse count.")
    selected = slice(max(0, manifest.pulse_count - trace_limit), manifest.pulse_count)
    return AcquisitionPreview(
        context=context,
        run_id=run_id,
        session_id=manifest.session_id,
        snapshot_id=manifest.snapshot_id,
        source_kind=contents.source_kind,
        source_id=contents.source_id,
        close_reason=manifest.close_reason.value,
        first_sequence=manifest.first_sequence,
        final_sequence=manifest.final_sequence,
        total_pulse_count=manifest.pulse_count,
        first_capture_unix_ns=manifest.first_capture_unix_ns,
        final_capture_unix_ns=manifest.final_capture_unix_ns,
        sample_rate_hz=contents.capture_config.sample_rate_hz,
        window_seconds=contents.capture_config.window_seconds,
        pretrigger_seconds=contents.capture_config.pretrigger_seconds,
        samples_v=contents.samples_v[selected].copy(),
        sequence=contents.sequence[selected].copy(),
        captured_at_unix_ns=contents.captured_at_unix_ns[selected].copy(),
        quality=contents.quality[selected].copy(),
    )