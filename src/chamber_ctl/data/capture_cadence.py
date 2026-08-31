from __future__ import annotations

import math
import json
import time
from collections import deque
from dataclasses import dataclass, replace
from enum import Enum
from typing import Iterable
from uuid import UUID


CADENCE_SCHEMA_VERSION = 1
DEFAULT_DISPLAY_HORIZON_SECONDS = 5.0
DEFAULT_ROLLING_WINDOW_SECONDS = 2.0
DEFAULT_SAMPLE_INTERVAL_SECONDS = 0.1
ROLLING_WINDOW_OPTIONS_SECONDS = (1.0, 2.0, 3.0)
MAX_CADENCE_PAYLOAD_BYTES = 262_144


class CadenceQuality(str, Enum):
    COUNTER_EXACT = "counter_exact"
    TIMESTAMP_INFERRED = "timestamp_inferred"
    UNAVAILABLE = "unavailable"


class GapConfidence(str, Enum):
    HIGH = "high"
    LOW = "low"


def _finite_positive(name: str, value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"{name} must be finite.")
    normalized = float(value)
    if normalized <= 0:
        raise ValueError(f"{name} must be positive.")
    return normalized


def _non_negative_integer(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer.")
    return value


@dataclass(frozen=True)
class PulseCadenceObservation:
    session_id: UUID
    sequence: int
    captured_at_unix_ns: int
    captured_at_monotonic_ns: int
    trigger_ordinal: int | None = None
    counter_epoch: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, UUID):
            raise ValueError("session_id must be a UUID.")
        for name in ("sequence", "captured_at_unix_ns", "captured_at_monotonic_ns"):
            _non_negative_integer(name, getattr(self, name))
        if self.trigger_ordinal is not None:
            _non_negative_integer("trigger_ordinal", self.trigger_ordinal)
            if not isinstance(self.counter_epoch, str) or not self.counter_epoch.strip():
                raise ValueError("counter_epoch must identify an available trigger ordinal.")
        elif self.counter_epoch is not None:
            raise ValueError("counter_epoch requires a trigger ordinal.")


@dataclass(frozen=True)
class CadenceGap:
    sequence_before: int
    sequence_after: int
    closed_at_unix_ns: int
    closed_at_monotonic_ns: int
    interval_seconds: float
    estimated_lost_count: int
    residual_seconds: float
    quality: CadenceQuality
    confidence: GapConfidence
    crosses_snapshot_boundary: bool = False

    def __post_init__(self) -> None:
        if self.sequence_after <= self.sequence_before:
            raise ValueError("Cadence gap sequences must increase.")
        _non_negative_integer("closed_at_unix_ns", self.closed_at_unix_ns)
        _non_negative_integer("closed_at_monotonic_ns", self.closed_at_monotonic_ns)
        _finite_positive("interval_seconds", self.interval_seconds)
        _non_negative_integer("estimated_lost_count", self.estimated_lost_count)
        if not math.isfinite(self.residual_seconds):
            raise ValueError("residual_seconds must be finite.")


@dataclass(frozen=True)
class RollingCadence:
    window_seconds: float
    capture_rate_hz: float | None
    estimated_lost_per_second: float | None
    captured_count: int
    estimated_lost_count: int

    def __post_init__(self) -> None:
        _finite_positive("window_seconds", self.window_seconds)
        _non_negative_integer("captured_count", self.captured_count)
        _non_negative_integer("estimated_lost_count", self.estimated_lost_count)
        for name in ("capture_rate_hz", "estimated_lost_per_second"):
            value = getattr(self, name)
            if value is not None and (not math.isfinite(value) or value < 0):
                raise ValueError(f"{name} must be finite and non-negative when available.")


@dataclass(frozen=True)
class LiveCadencePoint:
    sampled_at_monotonic_ns: int
    expected_rate_hz: float | None
    quality: CadenceQuality
    windows: tuple[RollingCadence, ...]
    provisional_lost_count: int

    def __post_init__(self) -> None:
        _non_negative_integer("sampled_at_monotonic_ns", self.sampled_at_monotonic_ns)
        if self.expected_rate_hz is not None:
            _finite_positive("expected_rate_hz", self.expected_rate_hz)
        _non_negative_integer("provisional_lost_count", self.provisional_lost_count)


@dataclass(frozen=True)
class LiveCadenceGap:
    gap: CadenceGap
    projected_monotonic_ns: int

    def __post_init__(self) -> None:
        _non_negative_integer("projected_monotonic_ns", self.projected_monotonic_ns)


@dataclass(frozen=True)
class LiveCadenceSnapshot:
    session_id: UUID | None
    quality: CadenceQuality
    expected_rate_hz: float | None
    rolling_window_options_seconds: tuple[float, ...]
    default_rolling_window_seconds: float
    display_horizon_seconds: float
    captured_count: int
    inferred_lost_count: int
    ambiguous_gap_count: int
    points: tuple[LiveCadencePoint, ...]
    gaps: tuple[LiveCadenceGap, ...]

    def encode(self, *, context: str | None = None, run_id: UUID | None = None) -> bytes:
        if context not in {None, "exposure", "diagnostic"}:
            raise ValueError("Live cadence context must be exposure or diagnostic when provided.")
        if run_id is not None and not isinstance(run_id, UUID):
            raise ValueError("Live cadence run_id must be a UUID when provided.")
        if context != "exposure" and run_id is not None:
            raise ValueError("Only exposure cadence may include a run ID.")
        latest_ns = self.points[-1].sampled_at_monotonic_ns if self.points else 0
        value = {
            "schema_version": CADENCE_SCHEMA_VERSION,
            "context": context,
            "run_id": None if run_id is None else str(run_id),
            "session_id": None if self.session_id is None else str(self.session_id),
            "quality": self.quality.value,
            "expected_rate_hz": self.expected_rate_hz,
            "rolling_window_options_seconds": list(self.rolling_window_options_seconds),
            "default_rolling_window_seconds": self.default_rolling_window_seconds,
            "display_horizon_seconds": self.display_horizon_seconds,
            "captured_count": self.captured_count,
            "inferred_lost_count": self.inferred_lost_count,
            "ambiguous_gap_count": self.ambiguous_gap_count,
            "points": [
                {
                    "relative_seconds": (point.sampled_at_monotonic_ns - latest_ns) / 1e9,
                    "expected_rate_hz": point.expected_rate_hz,
                    "quality": point.quality.value,
                    "provisional_lost_count": point.provisional_lost_count,
                    "windows": [
                        {
                            "window_seconds": window.window_seconds,
                            "capture_rate_hz": window.capture_rate_hz,
                            "estimated_lost_per_second": window.estimated_lost_per_second,
                            "captured_count": window.captured_count,
                            "estimated_lost_count": window.estimated_lost_count,
                        }
                        for window in point.windows
                    ],
                }
                for point in self.points
            ],
            "gaps": [
                {
                    "sequence_before": item.gap.sequence_before,
                    "sequence_after": item.gap.sequence_after,
                    "relative_seconds": (item.projected_monotonic_ns - latest_ns) / 1e9,
                    "interval_seconds": item.gap.interval_seconds,
                    "estimated_lost_count": item.gap.estimated_lost_count,
                    "residual_seconds": item.gap.residual_seconds,
                    "quality": item.gap.quality.value,
                    "confidence": item.gap.confidence.value,
                    "crosses_snapshot_boundary": item.gap.crosses_snapshot_boundary,
                }
                for item in self.gaps
                if item.gap.estimated_lost_count > 0
            ],
        }
        payload = json.dumps(value, allow_nan=False, separators=(",", ":")).encode("utf-8")
        if len(payload) > MAX_CADENCE_PAYLOAD_BYTES:
            raise ValueError("Live cadence payload exceeds its maximum encoded size.")
        return payload

@dataclass(frozen=True)
class DecodedCadencePoint:
    relative_seconds: float
    expected_rate_hz: float | None
    quality: CadenceQuality
    provisional_lost_count: int
    windows: tuple[RollingCadence, ...]


@dataclass(frozen=True)
class DecodedCadenceGap:
    sequence_before: int
    sequence_after: int
    relative_seconds: float
    interval_seconds: float
    estimated_lost_count: int
    residual_seconds: float
    quality: CadenceQuality
    confidence: GapConfidence
    crosses_snapshot_boundary: bool


@dataclass(frozen=True)
class DecodedLiveCadence:
    context: str | None
    run_id: UUID | None
    session_id: UUID | None
    quality: CadenceQuality
    expected_rate_hz: float | None
    rolling_window_options_seconds: tuple[float, ...]
    default_rolling_window_seconds: float
    display_horizon_seconds: float
    captured_count: int
    inferred_lost_count: int
    ambiguous_gap_count: int
    points: tuple[DecodedCadencePoint, ...]
    gaps: tuple[DecodedCadenceGap, ...]


def decode_live_cadence(payload: bytes) -> DecodedLiveCadence:
    if not isinstance(payload, bytes) or len(payload) > MAX_CADENCE_PAYLOAD_BYTES:
        raise ValueError("Live cadence payload must be bounded bytes.")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Live cadence payload must be UTF-8 JSON.") from exc
    expected = {
        "schema_version",
        "context",
        "run_id",
        "session_id",
        "quality",
        "expected_rate_hz",
        "rolling_window_options_seconds",
        "default_rolling_window_seconds",
        "display_horizon_seconds",
        "captured_count",
        "inferred_lost_count",
        "ambiguous_gap_count",
        "points",
        "gaps",
    }
    if not isinstance(value, dict) or set(value) != expected or value["schema_version"] != CADENCE_SCHEMA_VERSION:
        raise ValueError("Unsupported live cadence payload schema.")
    context = value["context"]
    if context not in {None, "exposure", "diagnostic"}:
        raise ValueError("Live cadence context is invalid.")
    run_id = None if value["run_id"] is None else UUID(str(value["run_id"]))
    if context != "exposure" and run_id is not None:
        raise ValueError("Only exposure cadence may include a run ID.")
    options_value = value["rolling_window_options_seconds"]
    if not isinstance(options_value, list):
        raise ValueError("Live cadence rolling-window options must be a list.")
    options = tuple(_finite_positive("rolling window", item) for item in options_value)
    if not options or tuple(sorted(set(options))) != options:
        raise ValueError("Live cadence rolling-window options must be unique and increasing.")
    default_window = _finite_positive("default_rolling_window_seconds", value["default_rolling_window_seconds"])
    if default_window not in options:
        raise ValueError("Default live cadence window is not an advertised option.")
    expected_rate = value["expected_rate_hz"]
    if expected_rate is not None:
        expected_rate = _finite_positive("expected_rate_hz", expected_rate)
    points_value = value["points"]
    gaps_value = value["gaps"]
    if not isinstance(points_value, list) or len(points_value) > 100:
        raise ValueError("Live cadence points must be a bounded list.")
    if not isinstance(gaps_value, list) or len(gaps_value) > 100:
        raise ValueError("Live cadence gaps must be a bounded list.")

    points = tuple(_decode_point(item, options) for item in points_value)
    gaps = tuple(_decode_gap(item) for item in gaps_value)
    if any(right.relative_seconds <= left.relative_seconds for left, right in zip(points, points[1:])):
        raise ValueError("Live cadence point times must increase.")
    horizon = _finite_positive("display_horizon_seconds", value["display_horizon_seconds"])
    if any(point.relative_seconds < -horizon - 1e-9 or point.relative_seconds > 1e-9 for point in points):
        raise ValueError("Live cadence point lies outside the display horizon.")
    return DecodedLiveCadence(
        context=context,
        run_id=run_id,
        session_id=None if value["session_id"] is None else UUID(str(value["session_id"])),
        quality=CadenceQuality(value["quality"]),
        expected_rate_hz=expected_rate,
        rolling_window_options_seconds=options,
        default_rolling_window_seconds=default_window,
        display_horizon_seconds=horizon,
        captured_count=_non_negative_integer("captured_count", value["captured_count"]),
        inferred_lost_count=_non_negative_integer("inferred_lost_count", value["inferred_lost_count"]),
        ambiguous_gap_count=_non_negative_integer("ambiguous_gap_count", value["ambiguous_gap_count"]),
        points=points,
        gaps=gaps,
    )


def _decode_point(value: object, options: tuple[float, ...]) -> DecodedCadencePoint:
    expected = {
        "relative_seconds",
        "expected_rate_hz",
        "quality",
        "provisional_lost_count",
        "windows",
    }
    if not isinstance(value, dict) or set(value) != expected or not isinstance(value["windows"], list):
        raise ValueError("Live cadence point contains unknown or missing fields.")
    relative = float(value["relative_seconds"])
    if not math.isfinite(relative):
        raise ValueError("Live cadence relative time must be finite.")
    expected_rate = value["expected_rate_hz"]
    if expected_rate is not None:
        expected_rate = _finite_positive("point expected_rate_hz", expected_rate)
    windows = tuple(_decode_window(item) for item in value["windows"])
    if tuple(item.window_seconds for item in windows) != options:
        raise ValueError("Live cadence point windows do not match advertised options.")
    return DecodedCadencePoint(
        relative,
        expected_rate,
        CadenceQuality(value["quality"]),
        _non_negative_integer("provisional_lost_count", value["provisional_lost_count"]),
        windows,
    )


def _decode_window(value: object) -> RollingCadence:
    expected = {
        "window_seconds",
        "capture_rate_hz",
        "estimated_lost_per_second",
        "captured_count",
        "estimated_lost_count",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("Live cadence window contains unknown or missing fields.")
    return RollingCadence(
        window_seconds=float(value["window_seconds"]),
        capture_rate_hz=None if value["capture_rate_hz"] is None else float(value["capture_rate_hz"]),
        estimated_lost_per_second=(
            None
            if value["estimated_lost_per_second"] is None
            else float(value["estimated_lost_per_second"])
        ),
        captured_count=value["captured_count"],
        estimated_lost_count=value["estimated_lost_count"],
    )


def _decode_gap(value: object) -> DecodedCadenceGap:
    expected = {
        "sequence_before",
        "sequence_after",
        "relative_seconds",
        "interval_seconds",
        "estimated_lost_count",
        "residual_seconds",
        "quality",
        "confidence",
        "crosses_snapshot_boundary",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("Live cadence gap contains unknown or missing fields.")
    relative = float(value["relative_seconds"])
    residual = float(value["residual_seconds"])
    if not math.isfinite(relative) or not math.isfinite(residual):
        raise ValueError("Live cadence gap coordinates must be finite.")
    crosses = value["crosses_snapshot_boundary"]
    if not isinstance(crosses, bool):
        raise ValueError("Live cadence snapshot-boundary flag must be boolean.")
    sequence_before = _non_negative_integer("sequence_before", value["sequence_before"])
    sequence_after = _non_negative_integer("sequence_after", value["sequence_after"])
    if sequence_after <= sequence_before:
        raise ValueError("Live cadence gap sequences must increase.")
    return DecodedCadenceGap(
        sequence_before=sequence_before,
        sequence_after=sequence_after,
        relative_seconds=relative,
        interval_seconds=_finite_positive("interval_seconds", value["interval_seconds"]),
        estimated_lost_count=_non_negative_integer("estimated_lost_count", value["estimated_lost_count"]),
        residual_seconds=residual,
        quality=CadenceQuality(value["quality"]),
        confidence=GapConfidence(value["confidence"]),
        crosses_snapshot_boundary=crosses,
    )


def observation_from_report(report) -> PulseCadenceObservation:
    return PulseCadenceObservation(
        session_id=report.session_id,
        sequence=report.sequence,
        captured_at_unix_ns=report.captured_at_unix_ns,
        captured_at_monotonic_ns=report.captured_at_monotonic_ns,
        trigger_ordinal=getattr(report, "trigger_ordinal", None),
        counter_epoch=getattr(report, "counter_epoch", None),
    )


def infer_gap(
    previous: PulseCadenceObservation,
    current: PulseCadenceObservation,
    expected_rate_hz: float,
    *,
    residual_tolerance_fraction: float = 0.25,
) -> CadenceGap:
    rate = _finite_positive("expected_rate_hz", expected_rate_hz)
    tolerance = _finite_positive("residual_tolerance_fraction", residual_tolerance_fraction)
    if tolerance >= 1:
        raise ValueError("residual_tolerance_fraction must be less than one.")
    if previous.session_id != current.session_id:
        raise ValueError("Cadence observations must belong to one session.")
    if current.sequence <= previous.sequence:
        raise ValueError("Cadence observation sequences must increase.")
    delta_ns = current.captured_at_monotonic_ns - previous.captured_at_monotonic_ns
    if delta_ns <= 0:
        raise ValueError("Cadence observation timestamps must increase.")

    interval_seconds = delta_ns / 1e9
    period_seconds = 1.0 / rate
    rounded_periods = max(1, int(math.floor(interval_seconds * rate + 0.5)))
    residual_seconds = interval_seconds - rounded_periods * period_seconds
    confidence = (
        GapConfidence.HIGH
        if abs(residual_seconds) <= period_seconds * tolerance
        else GapConfidence.LOW
    )

    exact_ordinals = (
        previous.trigger_ordinal is not None
        and current.trigger_ordinal is not None
        and previous.counter_epoch == current.counter_epoch
    )
    if exact_ordinals:
        assert previous.trigger_ordinal is not None and current.trigger_ordinal is not None
        if current.trigger_ordinal <= previous.trigger_ordinal:
            raise ValueError("Trigger ordinals must increase within one counter epoch.")
        lost_count = current.trigger_ordinal - previous.trigger_ordinal - 1
        quality = CadenceQuality.COUNTER_EXACT
    else:
        lost_count = rounded_periods - 1
        quality = CadenceQuality.TIMESTAMP_INFERRED

    return CadenceGap(
        sequence_before=previous.sequence,
        sequence_after=current.sequence,
        closed_at_unix_ns=current.captured_at_unix_ns,
        closed_at_monotonic_ns=current.captured_at_monotonic_ns,
        interval_seconds=interval_seconds,
        estimated_lost_count=lost_count,
        residual_seconds=residual_seconds,
        quality=quality,
        confidence=confidence,
    )


def infer_gaps(
    observations: Iterable[PulseCadenceObservation],
    expected_rate_hz: float,
    *,
    residual_tolerance_fraction: float = 0.25,
) -> tuple[CadenceGap, ...]:
    normalized = tuple(observations)
    return tuple(
        infer_gap(
            previous,
            current,
            expected_rate_hz,
            residual_tolerance_fraction=residual_tolerance_fraction,
        )
        for previous, current in zip(normalized, normalized[1:])
    )


@dataclass(frozen=True)
class _LiveObservation:
    observation: PulseCadenceObservation
    projected_monotonic_ns: int


class CaptureCadenceTracker:
    def __init__(
        self,
        *,
        rolling_windows_seconds: tuple[float, ...] = ROLLING_WINDOW_OPTIONS_SECONDS,
        display_horizon_seconds: float = DEFAULT_DISPLAY_HORIZON_SECONDS,
        sample_interval_seconds: float = DEFAULT_SAMPLE_INTERVAL_SECONDS,
        residual_tolerance_fraction: float = 0.25,
    ) -> None:
        windows = tuple(_finite_positive("rolling window", value) for value in rolling_windows_seconds)
        if not windows or tuple(sorted(set(windows))) != windows:
            raise ValueError("Rolling windows must be unique and increasing.")
        self.rolling_windows_seconds = windows
        self.display_horizon_seconds = _finite_positive("display_horizon_seconds", display_horizon_seconds)
        self.sample_interval_ns = int(_finite_positive("sample_interval_seconds", sample_interval_seconds) * 1e9)
        self.residual_tolerance_fraction = _finite_positive(
            "residual_tolerance_fraction",
            residual_tolerance_fraction,
        )
        if self.residual_tolerance_fraction >= 1:
            raise ValueError("residual_tolerance_fraction must be less than one.")
        self._retention_ns = int((display_horizon_seconds + max(windows)) * 1e9)
        self.reset()

    def reset(self, session_id: UUID | None = None) -> None:
        if session_id is not None and not isinstance(session_id, UUID):
            raise ValueError("session_id must be a UUID when provided.")
        self._session_id = session_id
        self._expected_rate_hz: float | None = None
        self._active_expected_rate_hz: float | None = None
        self._segment_pending = True
        self._observations: deque[_LiveObservation] = deque()
        self._gaps: deque[LiveCadenceGap] = deque()
        self._points: deque[LiveCadencePoint] = deque()
        self._segment_observations: deque[_LiveObservation] = deque()
        self._segment_gaps: deque[LiveCadenceGap] = deque()
        self._segment_anchor_board_ns: int | None = None
        self._segment_anchor_local_ns: int | None = None
        self._last_sample_ns: int | None = None
        self._captured_count = 0
        self._inferred_lost_count = 0
        self._ambiguous_gap_count = 0
        self._snapshot_boundaries: set[int] = set()

    @property
    def session_id(self) -> UUID | None:
        return self._session_id

    def set_expected_rate(self, expected_rate_hz: float | None) -> None:
        normalized = None if expected_rate_hz is None else _finite_positive("expected_rate_hz", expected_rate_hz)
        if normalized == self._active_expected_rate_hz:
            return
        self._active_expected_rate_hz = normalized
        if normalized is not None:
            self._expected_rate_hz = normalized
        self._segment_pending = True
        self._segment_observations.clear()
        self._segment_gaps.clear()
        self._segment_anchor_board_ns = None
        self._segment_anchor_local_ns = None

    def ingest(
        self,
        observation: PulseCadenceObservation,
        *,
        received_at_monotonic_ns: int | None = None,
    ) -> CadenceGap | None:
        received_ns = time.monotonic_ns() if received_at_monotonic_ns is None else received_at_monotonic_ns
        _non_negative_integer("received_at_monotonic_ns", received_ns)
        if self._session_id is None:
            self._session_id = observation.session_id
        elif observation.session_id != self._session_id:
            self.reset(observation.session_id)
        if self._observations and observation.sequence <= self._observations[-1].observation.sequence:
            raise ValueError("Cadence observations must arrive in increasing sequence order.")

        if self._segment_pending:
            self._segment_pending = False
            self._segment_anchor_board_ns = observation.captured_at_monotonic_ns
            self._segment_anchor_local_ns = received_ns
        assert self._segment_anchor_board_ns is not None and self._segment_anchor_local_ns is not None
        projected_ns = self._segment_anchor_local_ns + (
            observation.captured_at_monotonic_ns - self._segment_anchor_board_ns
        )
        live_observation = _LiveObservation(observation, projected_ns)
        gap = None
        if self._segment_observations and self._active_expected_rate_hz is not None:
            gap = infer_gap(
                self._segment_observations[-1].observation,
                observation,
                self._active_expected_rate_hz,
                residual_tolerance_fraction=self.residual_tolerance_fraction,
            )
            if any(gap.sequence_before <= boundary < gap.sequence_after for boundary in self._snapshot_boundaries):
                gap = replace(gap, crosses_snapshot_boundary=True)
            live_gap = LiveCadenceGap(gap, projected_ns)
            self._gaps.append(live_gap)
            self._segment_gaps.append(live_gap)
            self._inferred_lost_count += gap.estimated_lost_count
            if gap.confidence is GapConfidence.LOW:
                self._ambiguous_gap_count += 1
        self._observations.append(live_observation)
        self._segment_observations.append(live_observation)
        self._captured_count += 1
        self._prune(received_ns)
        return gap

    def mark_snapshot_boundary(self, final_sequence: int) -> None:
        boundary = _non_negative_integer("final_sequence", final_sequence)
        self._snapshot_boundaries.add(boundary)

        def annotate(items: deque[LiveCadenceGap]) -> deque[LiveCadenceGap]:
            return deque(
                replace(item, gap=replace(item.gap, crosses_snapshot_boundary=True))
                if item.gap.sequence_before <= boundary < item.gap.sequence_after
                else item
                for item in items
            )

        self._gaps = annotate(self._gaps)
        self._segment_gaps = annotate(self._segment_gaps)

    def sample(self, sampled_at_monotonic_ns: int | None = None) -> LiveCadencePoint:
        sampled_ns = time.monotonic_ns() if sampled_at_monotonic_ns is None else sampled_at_monotonic_ns
        _non_negative_integer("sampled_at_monotonic_ns", sampled_ns)
        provisional = self._provisional_lost_count(sampled_ns)
        windows = tuple(self._rolling_cadence(sampled_ns, window, provisional) for window in self.rolling_windows_seconds)
        quality = CadenceQuality.TIMESTAMP_INFERRED if self._expected_rate_hz is not None else CadenceQuality.UNAVAILABLE
        point = LiveCadencePoint(
            sampled_at_monotonic_ns=sampled_ns,
            expected_rate_hz=self._expected_rate_hz,
            quality=quality,
            windows=windows,
            provisional_lost_count=provisional,
        )
        if self._last_sample_ns is None or sampled_ns - self._last_sample_ns >= self.sample_interval_ns:
            self._points.append(point)
            self._last_sample_ns = sampled_ns
        elif self._points:
            self._points[-1] = point
        self._prune(sampled_ns)
        return point

    def snapshot(self, sampled_at_monotonic_ns: int | None = None) -> LiveCadenceSnapshot:
        point = self.sample(sampled_at_monotonic_ns)
        horizon_ns = int(self.display_horizon_seconds * 1e9)
        cutoff = point.sampled_at_monotonic_ns - horizon_ns
        points = tuple(item for item in self._points if item.sampled_at_monotonic_ns >= cutoff)
        gaps = tuple(item for item in self._gaps if item.projected_monotonic_ns >= cutoff)
        return LiveCadenceSnapshot(
            session_id=self._session_id,
            quality=point.quality,
            expected_rate_hz=self._expected_rate_hz,
            rolling_window_options_seconds=self.rolling_windows_seconds,
            default_rolling_window_seconds=DEFAULT_ROLLING_WINDOW_SECONDS,
            display_horizon_seconds=self.display_horizon_seconds,
            captured_count=self._captured_count,
            inferred_lost_count=self._inferred_lost_count,
            ambiguous_gap_count=self._ambiguous_gap_count,
            points=points,
            gaps=gaps,
        )

    def _provisional_lost_count(self, sampled_ns: int) -> int:
        if self._active_expected_rate_hz is None or not self._segment_observations:
            return 0
        elapsed_seconds = max(0, sampled_ns - self._segment_observations[-1].projected_monotonic_ns) / 1e9
        return max(0, int(math.floor(elapsed_seconds * self._active_expected_rate_hz - self.residual_tolerance_fraction)))

    def _rolling_cadence(self, sampled_ns: int, window_seconds: float, provisional: int) -> RollingCadence:
        if not self._segment_observations:
            return RollingCadence(window_seconds, None, None, 0, 0)
        segment_start = self._segment_observations[0].projected_monotonic_ns
        window_start = max(segment_start, sampled_ns - int(window_seconds * 1e9))
        duration_seconds = max(0, sampled_ns - window_start) / 1e9
        captured = sum(window_start < item.projected_monotonic_ns <= sampled_ns for item in self._segment_observations)
        confirmed_lost = sum(
            item.gap.estimated_lost_count
            for item in self._segment_gaps
            if window_start < item.projected_monotonic_ns <= sampled_ns
        )
        lost = confirmed_lost + provisional
        if duration_seconds <= 0:
            return RollingCadence(window_seconds, None, None, captured, lost)
        return RollingCadence(
            window_seconds=window_seconds,
            capture_rate_hz=captured / duration_seconds,
            estimated_lost_per_second=(lost / duration_seconds if self._active_expected_rate_hz is not None else None),
            captured_count=captured,
            estimated_lost_count=lost,
        )

    def _prune(self, now_ns: int) -> None:
        cutoff = now_ns - self._retention_ns
        while self._observations and self._observations[0].projected_monotonic_ns < cutoff:
            self._observations.popleft()
        while self._gaps and self._gaps[0].projected_monotonic_ns < cutoff:
            self._gaps.popleft()
        while self._points and self._points[0].sampled_at_monotonic_ns < cutoff:
            self._points.popleft()
        while len(self._segment_observations) > 1 and self._segment_observations[1].projected_monotonic_ns < cutoff:
            self._segment_observations.popleft()
        while self._segment_gaps and self._segment_gaps[0].projected_monotonic_ns < cutoff:
            self._segment_gaps.popleft()