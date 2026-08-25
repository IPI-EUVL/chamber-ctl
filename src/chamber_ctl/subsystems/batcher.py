from __future__ import annotations

import json
import math
import queue
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence, Set
from dataclasses import dataclass, field, replace
from enum import Enum
from types import MappingProxyType
from typing import Any, Protocol

from chamber_ctl.subsystems.exposure_controller import ExposureSettings


class TargetMode(str, Enum):
    DOSE = "dose"
    TIME = "time"


class ExecutionMode(str, Enum):
    AUTOMATIC = "automatic"
    MANUAL = "manual"


class ManifestStatus(str, Enum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    WITHDRAWN = "withdrawn"


class SampleProgressState(str, Enum):
    UNDER_TARGET = "under_target"
    WITHIN_TOLERANCE = "within_tolerance"
    OVERSHOT = "overshot"


class BatchDecisionKind(str, Enum):
    WAIT_CONTROLLER = "wait_controller"
    WAIT_ACTIVE = "wait_active"
    WAIT_FINALIZATION = "wait_finalization"
    REPAIR_METRICS = "repair_metrics"
    PAUSE_ERROR = "pause_error"
    PAUSE_FAILURE = "pause_failure"
    PAUSED = "paused"
    START_REMAINDER = "start_remainder"
    COMPLETE = "complete"


@dataclass(frozen=True)
class ExposureTemplate:
    name: str
    description: str = ""
    operator: str = ""
    zr_filter: str = ""
    sample_type: str = ""
    base_pressure: float = 0.0
    operating_pressure: float = 0.0
    flow_sccm: float = 0.0
    calibration_profile_id: str = ""
    calibration_revision: int = 0
    chopper_frequency_hz: float | None = 192.0

    def __post_init__(self) -> None:
        for field_name in ("base_pressure", "operating_pressure", "flow_sccm"):
            value = getattr(self, field_name)
            if not _is_finite_number(value):
                raise ValueError(f"{field_name} must be a finite number.")
        if not isinstance(self.calibration_profile_id, str):
            raise ValueError("calibration_profile_id must be text.")
        if isinstance(self.calibration_revision, bool) or not isinstance(self.calibration_revision, int) or self.calibration_revision < 0:
            raise ValueError("calibration_revision must be a non-negative integer.")
        if self.chopper_frequency_hz is not None and not _is_finite_number(self.chopper_frequency_hz):
            raise ValueError("chopper_frequency_hz must be finite when provided.")
        if self.chopper_frequency_hz is not None and float(self.chopper_frequency_hz) <= 0:
            raise ValueError("chopper_frequency_hz must be positive when provided.")

    def settings_for(self, entry: "BatchPlanEntry", target: float) -> ExposureSettings:
        if not math.isfinite(target) or target <= 0:
            raise ValueError("Derived exposure target must be a finite positive number.")

        values = {
            "name": self.name,
            "description": self.description,
            "operator": self.operator,
            "zr_filter": self.zr_filter,
            "sample_type": self.sample_type,
            "base_pressure": self.base_pressure,
            "operating_pressure": self.operating_pressure,
            "flow_sccm": self.flow_sccm,
            "calibration_profile_id": self.calibration_profile_id,
            "calibration_revision": self.calibration_revision,
            "chopper_frequency_hz": self.chopper_frequency_hz,
        }
        values.update(entry.overrides)
        settings = ExposureSettings(
            target_time=target if entry.mode is TargetMode.TIME else 0.0,
            target_dose=target if entry.mode is TargetMode.DOSE else 0.0,
            operator=values["operator"],
            zr_filter=values["zr_filter"],
            sample=str(entry.sample),
            sample_type=values["sample_type"],
            base_pressure=values["base_pressure"],
            operating_pressure=values["operating_pressure"],
            flow_sccm=values["flow_sccm"],
            calibration_profile_id=values["calibration_profile_id"],
            calibration_revision=values["calibration_revision"],
            chopper_frequency_hz=values["chopper_frequency_hz"],
        )
        settings.set_attr("name", values["name"])
        settings.set_attr("description", values["description"])
        return settings


_STRING_OVERRIDE_FIELDS = frozenset(("name", "description", "operator", "zr_filter", "sample_type"))
_NUMERIC_OVERRIDE_FIELDS = frozenset(("base_pressure", "operating_pressure", "flow_sccm"))
_OVERRIDE_FIELDS = _STRING_OVERRIDE_FIELDS | _NUMERIC_OVERRIDE_FIELDS


def _normalized_overrides(value: Mapping[str, Any]) -> Mapping[str, str | float]:
    if not isinstance(value, Mapping):
        raise ValueError("Entry overrides must be a mapping.")
    unknown = set(value) - _OVERRIDE_FIELDS
    if unknown:
        raise ValueError(f"Entry overrides contain unsupported fields: {', '.join(sorted(unknown))}.")
    normalized: dict[str, str | float] = {}
    for key, override in value.items():
        if key in _STRING_OVERRIDE_FIELDS:
            if not isinstance(override, str):
                raise ValueError(f"Override {key} must be a string.")
            normalized[key] = override
        else:
            if not _is_finite_number(override):
                raise ValueError(f"Override {key} must be a finite number.")
            normalized[key] = float(override)
    return MappingProxyType(normalized)


@dataclass(frozen=True)
class BatchPlanEntry:
    sample: int
    mode: TargetMode
    target: float
    overrides: Mapping[str, str | float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.sample, bool) or not isinstance(self.sample, int) or not 0 <= self.sample < 12:
            raise ValueError("Sample must be a zero-based index from 0 through 11.")
        try:
            normalized_mode = TargetMode(self.mode)
        except ValueError as exc:
            raise ValueError("Target mode must be 'dose' or 'time'.") from exc
        object.__setattr__(self, "mode", normalized_mode)
        if not _is_finite_number(self.target):
            raise ValueError("Target must be a finite number.")
        if float(self.target) < 0 or (normalized_mode is TargetMode.TIME and float(self.target) == 0):
            raise ValueError("Dose targets may be zero controls; time targets must be positive.")
        object.__setattr__(self, "target", float(self.target))
        object.__setattr__(self, "overrides", _normalized_overrides(self.overrides))

    @property
    def is_control(self) -> bool:
        return self.mode is TargetMode.DOSE and self.target == 0.0


@dataclass(frozen=True)
class BatchPlan:
    template: ExposureTemplate
    entries: tuple[BatchPlanEntry, ...]

    def __post_init__(self) -> None:
        entries = tuple(self.entries)
        samples = [entry.sample for entry in entries]
        if len(samples) != len(set(samples)):
            raise ValueError("A batch plan may contain only one entry per sample.")
        object.__setattr__(self, "entries", entries)


BATCH_PLAN_SCHEMA_VERSION = 3
BATCH_MANIFEST_SCHEMA_VERSION = 1


def batch_plan_to_dict(plan: BatchPlan) -> dict[str, Any]:
    return {
        "schema_version": BATCH_PLAN_SCHEMA_VERSION,
        "template": {
            "name": plan.template.name,
            "description": plan.template.description,
            "operator": plan.template.operator,
            "zr_filter": plan.template.zr_filter,
            "sample_type": plan.template.sample_type,
            "base_pressure": plan.template.base_pressure,
            "operating_pressure": plan.template.operating_pressure,
            "flow_sccm": plan.template.flow_sccm,
            "calibration_profile_id": plan.template.calibration_profile_id,
            "calibration_revision": plan.template.calibration_revision,
            "chopper_frequency_hz": plan.template.chopper_frequency_hz,
        },
        "entries": [
            {
                "sample": entry.sample,
                "mode": entry.mode.value,
                "target": entry.target,
                "overrides": dict(entry.overrides),
            }
            for entry in plan.entries
        ],
    }


def batch_plan_from_dict(value: object) -> BatchPlan:
    if not isinstance(value, dict) or value.get("schema_version") not in (1, 2, BATCH_PLAN_SCHEMA_VERSION):
        raise ValueError("Unsupported or missing batch plan schema version.")
    if set(value) != {"schema_version", "template", "entries"}:
        raise ValueError("Batch plan contains unknown or missing fields.")
    template = value["template"]
    entries = value["entries"]
    if not isinstance(template, dict) or not isinstance(entries, list):
        raise ValueError("Batch plan template and entries have invalid types.")
    required_template = {
        "name",
        "description",
        "operator",
        "zr_filter",
        "sample_type",
        "base_pressure",
        "operating_pressure",
        "flow_sccm",
    }
    if value["schema_version"] == BATCH_PLAN_SCHEMA_VERSION:
        required_template |= {"calibration_profile_id", "calibration_revision", "chopper_frequency_hz"}
    if set(template) != required_template:
        raise ValueError("Batch plan template contains unknown or missing fields.")
    schema_version = value["schema_version"]
    parsed_entries = []
    for entry in entries:
        expected_fields = {"sample", "mode", "target"}
        if schema_version >= 2:
            expected_fields.add("overrides")
        if not isinstance(entry, dict) or set(entry) != expected_fields:
            raise ValueError("Batch plan entry contains unknown or missing fields.")
        parsed_entries.append(
            BatchPlanEntry(
                entry["sample"],
                TargetMode(entry["mode"]),
                entry["target"],
                {} if schema_version == 1 else entry["overrides"],
            )
        )
    return BatchPlan(
        ExposureTemplate(
            name=str(template["name"]),
            description=str(template["description"]),
            operator=str(template["operator"]),
            zr_filter=str(template["zr_filter"]),
            sample_type=str(template["sample_type"]),
            base_pressure=template["base_pressure"],
            operating_pressure=template["operating_pressure"],
            flow_sccm=template["flow_sccm"],
            calibration_profile_id="" if schema_version < BATCH_PLAN_SCHEMA_VERSION else str(template["calibration_profile_id"]),
            calibration_revision=0 if schema_version < BATCH_PLAN_SCHEMA_VERSION else int(template["calibration_revision"]),
            chopper_frequency_hz=None if schema_version < BATCH_PLAN_SCHEMA_VERSION else template["chopper_frequency_hz"],
        ),
        tuple(parsed_entries),
    )


@dataclass(frozen=True)
class BatchManifest:
    batch_uuid: uuid.UUID
    revision: int
    status: ManifestStatus
    mode: ExecutionMode
    plan: BatchPlan
    created_at: float
    updated_at: float
    origin: str = "local"
    submitted_by: str = ""
    paused: bool = True
    cancel_pending: bool = False
    acknowledged_runs: tuple[uuid.UUID, ...] = ()
    revision_note: str = ""

    def __post_init__(self) -> None:
        if self.revision < 1:
            raise ValueError("Manifest revision must be at least one.")
        object.__setattr__(self, "status", ManifestStatus(self.status))
        object.__setattr__(self, "mode", ExecutionMode(self.mode))
        if not _is_finite_number(self.created_at) or not _is_finite_number(self.updated_at):
            raise ValueError("Manifest timestamps must be finite.")
        if self.updated_at < self.created_at:
            raise ValueError("Manifest update time cannot precede creation time.")
        if not self.origin.strip():
            raise ValueError("Manifest origin cannot be empty.")
        object.__setattr__(self, "acknowledged_runs", tuple(self.acknowledged_runs))


def batch_manifest_to_dict(manifest: BatchManifest) -> dict[str, Any]:
    return {
        "schema_version": BATCH_MANIFEST_SCHEMA_VERSION,
        "batch_uuid": str(manifest.batch_uuid),
        "revision": manifest.revision,
        "status": manifest.status.value,
        "mode": manifest.mode.value,
        "plan": batch_plan_to_dict(manifest.plan),
        "created_at": manifest.created_at,
        "updated_at": manifest.updated_at,
        "origin": manifest.origin,
        "submitted_by": manifest.submitted_by,
        "paused": manifest.paused,
        "cancel_pending": manifest.cancel_pending,
        "acknowledged_runs": [str(run_uuid) for run_uuid in manifest.acknowledged_runs],
        "revision_note": manifest.revision_note,
    }


def batch_manifest_from_dict(value: object) -> BatchManifest:
    expected = {
        "schema_version",
        "batch_uuid",
        "revision",
        "status",
        "mode",
        "plan",
        "created_at",
        "updated_at",
        "origin",
        "submitted_by",
        "paused",
        "cancel_pending",
        "acknowledged_runs",
        "revision_note",
    }
    if not isinstance(value, dict) or value.get("schema_version") != BATCH_MANIFEST_SCHEMA_VERSION:
        raise ValueError("Unsupported or missing batch manifest schema version.")
    if set(value) != expected:
        raise ValueError("Batch manifest contains unknown or missing fields.")
    if not isinstance(value["acknowledged_runs"], list):
        raise ValueError("Manifest acknowledged runs must be a list.")
    return BatchManifest(
        batch_uuid=uuid.UUID(str(value["batch_uuid"])),
        revision=int(value["revision"]),
        status=ManifestStatus(value["status"]),
        mode=ExecutionMode(value["mode"]),
        plan=batch_plan_from_dict(value["plan"]),
        created_at=float(value["created_at"]),
        updated_at=float(value["updated_at"]),
        origin=str(value["origin"]),
        submitted_by=str(value["submitted_by"]),
        paused=bool(value["paused"]),
        cancel_pending=bool(value["cancel_pending"]),
        acknowledged_runs=tuple(uuid.UUID(str(item)) for item in value["acknowledged_runs"]),
        revision_note=str(value["revision_note"]),
    )


def encode_batch_manifest(manifest: BatchManifest) -> bytes:
    return json.dumps(batch_manifest_to_dict(manifest), allow_nan=False, separators=(",", ":")).encode("utf-8")


def decode_batch_manifest(payload: bytes) -> BatchManifest:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Batch manifest is not valid UTF-8 JSON.") from exc
    return batch_manifest_from_dict(value)


class PlanGenerator(Protocol):
    @property
    def name(self) -> str: ...

    def generate(self, samples: Sequence[int]) -> tuple[BatchPlanEntry, ...]: ...


def _ordered_samples(samples: Sequence[int]) -> tuple[int, ...]:
    normalized = tuple(samples)
    if not normalized:
        raise ValueError("Select at least one sample.")
    if len(normalized) != len(set(normalized)):
        raise ValueError("Generator sample selection cannot contain duplicates.")
    for sample in normalized:
        if isinstance(sample, bool) or not isinstance(sample, int) or not 0 <= sample < 12:
            raise ValueError("Generator samples must be zero-based indices from 0 through 11.")
    return normalized


@dataclass(frozen=True)
class TargetAssignmentGenerator:
    mode: TargetMode
    target: float

    @property
    def name(self) -> str:
        return "target_assignment"

    def generate(self, samples: Sequence[int]) -> tuple[BatchPlanEntry, ...]:
        return tuple(BatchPlanEntry(sample, self.mode, self.target) for sample in _ordered_samples(samples))


@dataclass(frozen=True)
class LinearContrastDoseGenerator:
    minimum_dose: float
    maximum_dose: float

    @property
    def name(self) -> str:
        return "linear_contrast_dose"

    def generate(self, samples: Sequence[int]) -> tuple[BatchPlanEntry, ...]:
        ordered = _ordered_samples(samples)
        if not _is_finite_number(self.minimum_dose) or not _is_finite_number(self.maximum_dose):
            raise ValueError("Contrast doses must be finite.")
        if self.minimum_dose <= 0 or self.maximum_dose <= 0 or self.minimum_dose > self.maximum_dose:
            raise ValueError("Contrast doses must be positive with minimum no greater than maximum.")
        if len(ordered) == 1 and self.minimum_dose != self.maximum_dose:
            raise ValueError("A one-sample contrast range requires equal minimum and maximum doses.")
        if len(ordered) == 1:
            doses = (float(self.minimum_dose),)
        else:
            step = (float(self.maximum_dose) - float(self.minimum_dose)) / (len(ordered) - 1)
            doses = tuple(float(self.minimum_dose) + step * index for index in range(len(ordered)))
        return tuple(
            BatchPlanEntry(sample, TargetMode.DOSE, dose)
            for sample, dose in zip(ordered, doses)
        )


@dataclass(frozen=True)
class ControlGenerator:
    @property
    def name(self) -> str:
        return "zero_dose_control"

    def generate(self, samples: Sequence[int]) -> tuple[BatchPlanEntry, ...]:
        return tuple(BatchPlanEntry(sample, TargetMode.DOSE, 0.0) for sample in _ordered_samples(samples))


@dataclass(frozen=True)
class GeneratorApplication:
    plan: BatchPlan
    generator_name: str
    samples: tuple[int, ...]
    generated_entries: tuple[BatchPlanEntry, ...]


def apply_plan_generator(
    plan: BatchPlan,
    generator: PlanGenerator,
    samples: Sequence[int],
) -> GeneratorApplication:
    ordered = _ordered_samples(samples)
    existing_by_sample = {entry.sample: entry for entry in plan.entries}
    generated = tuple(
        replace(entry, overrides=existing_by_sample.get(entry.sample, entry).overrides)
        for entry in generator.generate(ordered)
    )
    selected = frozenset(ordered)
    retained = tuple(entry for entry in plan.entries if entry.sample not in selected)
    updated = BatchPlan(plan.template, retained + generated)
    return GeneratorApplication(updated, generator.name, ordered, generated)


@dataclass(frozen=True)
class ExposureAttempt:
    run_uuid: uuid.UUID
    sample: int | None
    created_at: float
    end_time: float | None = None
    status: str | None = None
    end_reason: str | None = None
    dose: float | None = None
    runtime: float | None = None
    snapshot_count: int = 0
    validation_error: str | None = None


@dataclass(frozen=True)
class ControllerSnapshot:
    available: bool
    idle: bool
    current_run_uuid: uuid.UUID | None = None
    error: str | None = None

    @classmethod
    def stopped(cls) -> "ControllerSnapshot":
        return cls(available=True, idle=True)

    @classmethod
    def active(cls, run_uuid: uuid.UUID) -> "ControllerSnapshot":
        return cls(available=True, idle=False, current_run_uuid=run_uuid)

    @classmethod
    def unavailable(cls, error: str) -> "ControllerSnapshot":
        return cls(available=False, idle=False, error=error)


@dataclass(frozen=True)
class AssessmentConfig:
    finalization_delay: float = 5.0
    dose_tolerance: float = 1.0
    time_tolerance: float = 5.0
    minimum_dose: float = -0.1
    minimum_runtime: float = -0.1

    def __post_init__(self) -> None:
        for field_name in ("finalization_delay", "dose_tolerance", "time_tolerance"):
            value = getattr(self, field_name)
            if not _is_finite_number(value) or float(value) < 0:
                raise ValueError(f"{field_name} must be a finite non-negative number.")


@dataclass(frozen=True)
class SampleProgress:
    sample: int
    mode: TargetMode
    target: float
    tolerance: float
    cumulative_dose: float
    cumulative_runtime: float
    attempt_count: int
    state: SampleProgressState

    @property
    def actual(self) -> float:
        return self.cumulative_dose if self.mode is TargetMode.DOSE else self.cumulative_runtime

    @property
    def remainder(self) -> float:
        return max(0.0, self.target - self.actual)

    @property
    def overshoot(self) -> float:
        return max(0.0, self.actual - self.target)


@dataclass(frozen=True)
class BatchDecision:
    kind: BatchDecisionKind
    message: str
    run_uuid: uuid.UUID | None = None
    sample: int | None = None
    settings: ExposureSettings | None = None


@dataclass(frozen=True)
class BatchAssessment:
    progress: tuple[SampleProgress, ...]
    decision: BatchDecision


def _is_finite_number(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(float(value))


def _decision(
    progress: tuple[SampleProgress, ...],
    kind: BatchDecisionKind,
    message: str,
    *,
    attempt: ExposureAttempt | None = None,
    sample: int | None = None,
    settings: ExposureSettings | None = None,
) -> BatchAssessment:
    return BatchAssessment(
        progress=progress,
        decision=BatchDecision(
            kind=kind,
            message=message,
            run_uuid=attempt.run_uuid if attempt is not None else None,
            sample=attempt.sample if attempt is not None else sample,
            settings=settings,
        ),
    )


def _progress_for(
    plan: BatchPlan,
    attempts: Iterable[ExposureAttempt],
    config: AssessmentConfig,
) -> tuple[SampleProgress, ...]:
    attempts_by_sample: dict[int, list[ExposureAttempt]] = {entry.sample: [] for entry in plan.entries}
    for attempt in attempts:
        if attempt.sample in attempts_by_sample:
            attempts_by_sample[attempt.sample].append(attempt)

    progress = []
    for entry in plan.entries:
        sample_attempts = attempts_by_sample[entry.sample]
        cumulative_dose = sum(float(attempt.dose) for attempt in sample_attempts if attempt.dose is not None)
        cumulative_runtime = sum(float(attempt.runtime) for attempt in sample_attempts if attempt.runtime is not None)
        actual = cumulative_dose if entry.mode is TargetMode.DOSE else cumulative_runtime
        tolerance = config.dose_tolerance if entry.mode is TargetMode.DOSE else config.time_tolerance
        if actual < entry.target - tolerance:
            state = SampleProgressState.UNDER_TARGET
        elif actual > entry.target + tolerance:
            state = SampleProgressState.OVERSHOT
        else:
            state = SampleProgressState.WITHIN_TOLERANCE
        progress.append(
            SampleProgress(
                sample=entry.sample,
                mode=entry.mode,
                target=entry.target,
                tolerance=tolerance,
                cumulative_dose=cumulative_dose,
                cumulative_runtime=cumulative_runtime,
                attempt_count=len(sample_attempts),
                state=state,
            )
        )
    return tuple(progress)


def _attempt_error(attempt: ExposureAttempt, planned_samples: Set[int]) -> str | None:
    if attempt.validation_error:
        return attempt.validation_error
    if isinstance(attempt.sample, bool) or not isinstance(attempt.sample, int) or attempt.sample not in planned_samples:
        return f"Run {attempt.run_uuid} has unknown sample {attempt.sample!r}."
    if not _is_finite_number(attempt.created_at):
        return f"Run {attempt.run_uuid} has an invalid creation time."
    if isinstance(attempt.snapshot_count, bool) or not isinstance(attempt.snapshot_count, int) or attempt.snapshot_count < 0:
        return f"Run {attempt.run_uuid} has an invalid snapshot count."
    if (attempt.status is None) != (attempt.end_time is None):
        return f"Run {attempt.run_uuid} has inconsistent terminal metadata."
    if attempt.end_time is not None and not _is_finite_number(attempt.end_time):
        return f"Run {attempt.run_uuid} has an invalid end time."
    return None


def _metrics_need_repair(attempt: ExposureAttempt) -> bool:
    if attempt.dose is None or attempt.runtime is None:
        return True
    return attempt.snapshot_count > 0 and (float(attempt.dose) == 0.0 or float(attempt.runtime) == 0.0)


def assess_batch(
    plan: BatchPlan,
    attempts: Sequence[ExposureAttempt],
    controller: ControllerSnapshot,
    *,
    now: float,
    acknowledged_runs: Set[uuid.UUID] = frozenset(),
    blocked_repair_runs: Set[uuid.UUID] = frozenset(),
    manually_paused: bool = False,
    config: AssessmentConfig = AssessmentConfig(),
) -> BatchAssessment:
    """Derive the next batch action without retaining planner progress."""
    if not _is_finite_number(now):
        raise ValueError("Current time must be finite.")

    planned_samples = frozenset(entry.sample for entry in plan.entries)
    ordered = sorted(attempts, key=lambda attempt: (attempt.created_at, str(attempt.run_uuid)))
    empty_progress = _progress_for(plan, (), config)

    for attempt in ordered:
        error = _attempt_error(attempt, planned_samples)
        if error is not None:
            return _decision(empty_progress, BatchDecisionKind.PAUSE_ERROR, error, attempt=attempt)

    active = [attempt for attempt in ordered if attempt.status is None]
    terminal = [attempt for attempt in ordered if attempt.status is not None]
    terminal_progress = _progress_for(plan, terminal, config)
    if len(active) > 1:
        return _decision(
            terminal_progress,
            BatchDecisionKind.PAUSE_ERROR,
            "More than one matching batch run is unterminated.",
            attempt=active[-1],
        )
    if active:
        attempt = active[0]
        if not controller.available:
            return _decision(
                terminal_progress,
                BatchDecisionKind.WAIT_CONTROLLER,
                controller.error or "Waiting for exposure-controller state.",
                attempt=attempt,
            )
        if not controller.idle and controller.current_run_uuid == attempt.run_uuid:
            return _decision(
                terminal_progress,
                BatchDecisionKind.WAIT_ACTIVE,
                f"Batch run {attempt.run_uuid} is still active.",
                attempt=attempt,
            )
        return _decision(
            terminal_progress,
            BatchDecisionKind.PAUSE_ERROR,
            f"Batch run {attempt.run_uuid} is unterminated but is not the current controller run.",
            attempt=attempt,
        )

    finalizing = [
        attempt
        for attempt in terminal
        if attempt.end_time is not None and float(now) < float(attempt.end_time) + config.finalization_delay
    ]
    if finalizing:
        attempt = max(finalizing, key=lambda item: (float(item.end_time), item.created_at, str(item.run_uuid)))
        remaining = float(attempt.end_time) + config.finalization_delay - float(now)
        return _decision(
            empty_progress,
            BatchDecisionKind.WAIT_FINALIZATION,
            f"Waiting {remaining:.1f}s for run {attempt.run_uuid} data finalization.",
            attempt=attempt,
        )

    for attempt in terminal:
        status = str(attempt.status).strip().upper()
        if status not in ("STOPPED", "ABORTED"):
            return _decision(
                empty_progress,
                BatchDecisionKind.PAUSE_ERROR,
                f"Run {attempt.run_uuid} has unknown terminal status {attempt.status!r}.",
                attempt=attempt,
            )

    for attempt in terminal:
        if _metrics_need_repair(attempt):
            if attempt.run_uuid in blocked_repair_runs:
                return _decision(
                    empty_progress,
                    BatchDecisionKind.PAUSE_ERROR,
                    f"Metric repair for run {attempt.run_uuid} exhausted its retry limit.",
                    attempt=attempt,
                )
            return _decision(
                empty_progress,
                BatchDecisionKind.REPAIR_METRICS,
                f"Run {attempt.run_uuid} needs dose and runtime recalculation.",
                attempt=attempt,
            )

    for attempt in terminal:
        if not _is_finite_number(attempt.dose) or not _is_finite_number(attempt.runtime):
            return _decision(
                empty_progress,
                BatchDecisionKind.PAUSE_ERROR,
                f"Run {attempt.run_uuid} has non-finite dose or runtime.",
                attempt=attempt,
            )
        if float(attempt.dose) < config.minimum_dose:
            return _decision(
                empty_progress,
                BatchDecisionKind.PAUSE_ERROR,
                f"Run {attempt.run_uuid} dose {attempt.dose} is below {config.minimum_dose} mJ/cm2.",
                attempt=attempt,
            )
        if float(attempt.runtime) < config.minimum_runtime:
            return _decision(
                empty_progress,
                BatchDecisionKind.PAUSE_ERROR,
                f"Run {attempt.run_uuid} runtime {attempt.runtime} is below {config.minimum_runtime}s.",
                attempt=attempt,
            )

    progress = _progress_for(plan, terminal, config)
    latest_by_sample: dict[int, ExposureAttempt] = {}
    for attempt in terminal:
        latest_by_sample[int(attempt.sample)] = attempt

    abort_blockers = [
        attempt
        for attempt in latest_by_sample.values()
        if str(attempt.status).strip().upper() == "ABORTED" and attempt.run_uuid not in acknowledged_runs
    ]
    if abort_blockers:
        attempt = max(abort_blockers, key=lambda item: (float(item.end_time), item.created_at, str(item.run_uuid)))
        return _decision(
            progress,
            BatchDecisionKind.PAUSE_FAILURE,
            f"Run {attempt.run_uuid} ended ABORTED: {attempt.end_reason or 'no reason recorded'}",
            attempt=attempt,
        )

    first_incomplete = next(
        (item for item in progress if item.state is SampleProgressState.UNDER_TARGET),
        None,
    )
    if first_incomplete is not None:
        latest = latest_by_sample.get(first_incomplete.sample)
        if (
            latest is not None
            and str(latest.status).strip().upper() == "STOPPED"
            and latest.run_uuid not in acknowledged_runs
        ):
            return _decision(
                progress,
                BatchDecisionKind.PAUSE_FAILURE,
                f"Sample {first_incomplete.sample + 1} remains below its plan target after run {latest.run_uuid}.",
                attempt=latest,
            )

    if manually_paused:
        return _decision(progress, BatchDecisionKind.PAUSED, "Batch is manually paused.")

    if first_incomplete is None:
        return _decision(progress, BatchDecisionKind.COMPLETE, "Every sample met or overshot its plan target.")

    if not controller.available:
        return _decision(
            progress,
            BatchDecisionKind.WAIT_CONTROLLER,
            controller.error or "Waiting for exposure-controller state.",
        )
    if not controller.idle or controller.current_run_uuid is not None:
        return _decision(
            progress,
            BatchDecisionKind.WAIT_CONTROLLER,
            "Exposure controller is busy.",
        )

    entry = next(entry for entry in plan.entries if entry.sample == first_incomplete.sample)
    settings = plan.template.settings_for(entry, first_incomplete.remainder)
    return _decision(
        progress,
        BatchDecisionKind.START_REMAINDER,
        f"Sample {entry.sample + 1} needs {first_incomplete.remainder:.6g} {entry.mode.value}.",
        sample=entry.sample,
        settings=settings,
    )


class BatchHistorySource(Protocol):
    def load_attempts(self, batch_uuid: uuid.UUID) -> tuple[ExposureAttempt, ...]: ...

    def repair_metrics(self, run_uuid: uuid.UUID) -> tuple[float, float]: ...

    def close(self) -> None: ...


class ControllerStateSource(Protocol):
    def snapshot(self) -> ControllerSnapshot: ...

    def acquire_automation(self, owner_name: str) -> str: ...

    def release_automation(self) -> str: ...

    def start_exposure(
        self,
        settings: ExposureSettings,
        batch_uuid: uuid.UUID,
        run_tags: Mapping[str, str | int | float] | None = None,
    ) -> str: ...

    def close(self) -> None: ...


def _optional_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sample_index(value: object) -> int | None:
    parsed = _optional_float(value)
    if parsed is None or not math.isfinite(parsed) or not parsed.is_integer():
        return None
    return int(parsed)


def _run_uuid_from_tags(tags: Mapping[str, Any]) -> uuid.UUID:
    try:
        return uuid.UUID(str(tags.get("run")))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("Exposure record has an invalid or missing run UUID.") from exc


def _attempt_from_record(record: Any) -> ExposureAttempt:
    tags: Mapping[str, Any] = record.get_tags() or {}
    run_uuid = _run_uuid_from_tags(tags)
    errors = []

    sample = _sample_index(tags.get("sample"))
    if sample is None:
        errors.append(f"Run {run_uuid} has invalid sample tag {tags.get('sample')!r}.")

    dose = _optional_float(tags.get("dose"))
    runtime = _optional_float(tags.get("runtime"))
    if tags.get("dose") not in (None, "") and dose is None:
        errors.append(f"Run {run_uuid} has invalid dose tag {tags.get('dose')!r}.")
    if tags.get("runtime") not in (None, "") and runtime is None:
        errors.append(f"Run {run_uuid} has invalid runtime tag {tags.get('runtime')!r}.")

    metadata = record.get_metadata() or {}
    created_at = _optional_float(metadata.get("created_at"))
    if created_at is None:
        created_at = _optional_float(record.get_record().get_timestamp())
    if created_at is None:
        created_at = math.nan

    end_metadata = record.get_end_metadata()
    if end_metadata is None:
        end_time = None
        status = None
        end_reason = None
    else:
        end_time = _optional_float(end_metadata.get("end_time"))
        status_value = end_metadata.get("status")
        status = None if status_value is None else str(status_value)
        reason_value = end_metadata.get("end_reason")
        end_reason = None if reason_value is None else str(reason_value)
        if end_time is None:
            errors.append(f"Run {run_uuid} has invalid terminal end time {end_metadata.get('end_time')!r}.")

    snapshot_count = sum(
        1
        for filename, resource_type in record.get_record().list_resources()
        if (resource_type == "snapshot" and filename.endswith(".npz"))
        or (resource_type == "euv_snapshot" and filename.endswith(".h5"))
    )
    return ExposureAttempt(
        run_uuid=run_uuid,
        sample=sample,
        created_at=created_at,
        end_time=end_time,
        status=status,
        end_reason=end_reason,
        dose=dose,
        runtime=runtime,
        snapshot_count=snapshot_count,
        validation_error=" ".join(errors) or None,
    )


class BatchHistoryStore:
    """Own all experiment-library objects on one worker thread."""

    def __init__(self, data_path: str, *, request_timeout: float = 120.0) -> None:
        self._data_path = data_path
        self._request_timeout = request_timeout
        self._commands: queue.Queue[tuple[str, object, queue.Queue]] = queue.Queue()
        self._ready: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)
        self._closed = False
        self._thread = threading.Thread(target=self._worker, name="batch-history-store", daemon=True)
        self._thread.start()
        try:
            ok, result = self._ready.get(timeout=10.0)
        except queue.Empty as exc:
            raise TimeoutError("Timed out while opening the experiment history store.") from exc
        if not ok:
            raise result

    def _worker(self) -> None:
        from ipi_ecs.subsystems.experiment_controller import ExperimentReader

        from chamber_ctl.data.dose_analysis import analyze_experiment_entry, write_analysis_revision, DoseAnalysisRevision

        experiment_reader = None
        try:
            experiment_reader = ExperimentReader(self._data_path, "exposure")
            self._ready.put((True, None))
            while True:
                operation, payload, response = self._commands.get()
                if operation == "close":
                    response.put((True, None))
                    return
                try:
                    if operation == "load":
                        records = experiment_reader.query(
                            {"tags": {"batch_uuid": str(payload)}},
                            limit=None,
                        )
                        value = tuple(_attempt_from_record(record) for record in records)
                    elif operation == "repair":
                        run_uuid = payload
                        record = experiment_reader.locate_run_by_uuid(run_uuid)
                        if record is None:
                            raise ValueError(f"Exposure run {run_uuid} was not found.")
                        result = analyze_experiment_entry(run_uuid, record.get_record())
                        dose = result.total_dose_mj_cm2
                        runtime = result.runtime_seconds
                        if not _is_finite_number(dose) or not _is_finite_number(runtime):
                            raise ValueError(f"Recalculation for run {run_uuid} returned non-finite metrics.")
                        write_analysis_revision(
                            record.get_record(),
                            DoseAnalysisRevision(uuid.uuid4(), time.time(), result),
                            promote=True,
                        )
                        value = (float(dose), float(runtime))
                    else:
                        raise ValueError(f"Unknown history-store operation {operation!r}.")
                except Exception as exc:
                    response.put((False, exc))
                else:
                    response.put((True, value))
        except Exception as exc:
            if self._ready.empty():
                self._ready.put((False, exc))
        finally:
            if experiment_reader is not None:
                experiment_reader.close()

    def _request(self, operation: str, payload: object = None):
        if self._closed:
            raise RuntimeError("Batch history store is closed.")
        response: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)
        self._commands.put((operation, payload, response))
        try:
            ok, result = response.get(timeout=self._request_timeout)
        except queue.Empty as exc:
            raise TimeoutError(f"Timed out waiting for history-store {operation}.") from exc
        if not ok:
            raise result
        return result

    def load_attempts(self, batch_uuid: uuid.UUID) -> tuple[ExposureAttempt, ...]:
        return self._request("load", batch_uuid)

    def repair_metrics(self, run_uuid: uuid.UUID) -> tuple[float, float]:
        return self._request("repair", run_uuid)

    def close(self) -> None:
        if self._closed:
            return
        self._request("close")
        self._closed = True
        self._thread.join(timeout=5.0)
        if self._thread.is_alive():
            raise TimeoutError("Timed out while closing the batch history store.")


class DdsControllerStateSource:
    """Read controller state and perform explicitly requested tagged starts."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        *,
        read_timeout: float = 5.0,
        start_timeout: float = 60.0,
        registered_handle=None,
        on_start_progress: Callable[[str], None] | None = None,
    ) -> None:
        import ipi_ecs.dds.client as client

        self._read_timeout = read_timeout
        self._start_timeout = start_timeout
        self._lock = threading.Lock()
        self._state_kv = None
        self._settings_kv = None
        self._prepare_event = None
        self._acquire_automation_event = None
        self._release_automation_event = None
        self._on_start_progress = on_start_progress
        self._closed = False
        self._configured = False
        self._owns_client = registered_handle is None
        self._client = None
        if registered_handle is not None:
            self._configure_handle(registered_handle)
        else:
            client_uuid = uuid.uuid4()
            self._client = client.DDSClient(client_uuid, ip=host)
            self._client.when_ready().then(self._on_ready)

    def _on_ready(self) -> None:
        with self._lock:
            if self._closed or self._configured:
                return
        handle = self._client.register_subsystem(
            f"__batch_planner_{uuid.uuid4()}",
            uuid.uuid4(),
            temporary=True,
        )
        self._configure_handle(handle)

    def _configure_handle(self, handle) -> None:
        import ipi_ecs.dds.subsystem as subsystem
        import ipi_ecs.dds.types as types

        from chamber_ctl.subsystems import uuids

        with self._lock:
            if self._closed or self._configured:
                return
            self._configured = True
            self._state_kv = handle.add_remote_kv(
                uuids.UUID_EXPOSURE_CONTROLLER,
                subsystem.KVDescriptor(types.ByteTypeSpecifier(), b"experiment_state", True, True, False),
            )
            self._settings_kv = handle.add_remote_kv(
                uuids.UUID_EXPOSURE_CONTROLLER,
                subsystem.KVDescriptor(types.ByteTypeSpecifier(), b"settings", False, True, True),
            )
            self._prepare_event = handle.add_event_provider(b"prepare_exposure_with_tags")
            self._acquire_automation_event = handle.add_event_provider(b"acquire_exposure_automation")
            self._release_automation_event = handle.add_event_provider(b"release_exposure_automation")

    @staticmethod
    def _decode(value: bytes) -> ControllerSnapshot:
        import segment_bytes
        from ipi_ecs.subsystems.experiment_controller import ExperimentController, RunState

        parts = segment_bytes.decode(value)
        if len(parts) != 2:
            raise ValueError("Exposure-controller state payload must contain phase and run state.")
        phase = int.from_bytes(parts[0], byteorder="big")
        if phase == ExperimentController.RUN_STATE_STOPPED:
            return ControllerSnapshot.stopped()
        if not parts[1]:
            raise ValueError("Active exposure-controller state omitted its run payload.")
        run = RunState.decode(parts[1].decode("utf-8"))
        return ControllerSnapshot.active(run.get_uuid())

    def snapshot(self) -> ControllerSnapshot:
        from ipi_ecs.cli.captive_cli import wait_for

        with self._lock:
            state_kv = self._state_kv
        if state_kv is None:
            return ControllerSnapshot.unavailable("DDS exposure-controller state is not connected yet.")
        try:
            awaiter = state_kv.try_get()
            if awaiter is None:
                raise RuntimeError("DDS state read could not be sent.")
            value, state, reason = wait_for(awaiter, timeout=self._read_timeout)
            if state is not None or value is None:
                raise RuntimeError(str(reason or "DDS state read was rejected."))
            return self._decode(value)
        except Exception as exc:
            return ControllerSnapshot.unavailable(f"{type(exc).__name__}: {exc}")

    @staticmethod
    def _response_text(value: object, fallback: str) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace") or fallback
        if value is None:
            return fallback
        return str(value)

    def _call_controller_event(
        self,
        event,
        payload: bytes,
        timeout: float,
        on_feedback: Callable[[str], None] | None = None,
    ) -> str:
        import ipi_ecs.dds.client as client

        from chamber_ctl.subsystems import uuids

        if event is None:
            raise RuntimeError("DDS exposure automation controls are not connected yet.")
        event_handle = event.call(payload, [uuids.UUID_EXPOSURE_CONTROLLER])
        if event_handle is None:
            raise RuntimeError("Failed to send exposure-controller event.")
        if on_feedback is not None:
            on_feedback("Exposure start request sent; awaiting controller feedback.")
        started_at = time.monotonic()
        last_update = None
        while event_handle.is_in_progress() and time.monotonic() - started_at < timeout:
            update = event_handle.get_last_update()
            if update != last_update:
                last_update = update
                feedback = event_handle.get_result(uuids.UUID_EXPOSURE_CONTROLLER)
                if feedback is not None and on_feedback is not None:
                    on_feedback(self._response_text(feedback, "Exposure start is in progress."))
            time.sleep(0.1)
        if event_handle.is_in_progress():
            raise TimeoutError("Timed out waiting for exposure-controller event response.")
        state = event_handle.get_state(uuids.UUID_EXPOSURE_CONTROLLER)
        result = event_handle.get_result(uuids.UUID_EXPOSURE_CONTROLLER)
        if state != client.EVENT_OK:
            message = self._response_text(result, "Exposure-controller event failed.")
            if on_feedback is not None:
                on_feedback(f"Exposure start failed: {message}")
            raise RuntimeError(message)
        message = self._response_text(result, "Exposure-controller event completed successfully.")
        if on_feedback is not None:
            on_feedback(message)
        return message

    def acquire_automation(self, owner_name: str) -> str:
        with self._lock:
            event = self._acquire_automation_event
        return self._call_controller_event(event, owner_name.encode("utf-8"), 10.0)

    def release_automation(self) -> str:
        with self._lock:
            event = self._release_automation_event
        return self._call_controller_event(event, bytes(), 10.0)

    def start_exposure(
        self,
        settings: ExposureSettings,
        batch_uuid: uuid.UUID,
        run_tags: Mapping[str, str | int | float] | None = None,
    ) -> str:
        import segment_bytes
        import ipi_ecs.dds.client as client
        import ipi_ecs.dds.magics as magics
        from ipi_ecs.cli.captive_cli import wait_for
        from ipi_ecs.subsystems.experiment_controller import encode_prepare_run_tags

        from chamber_ctl.subsystems import uuids

        with self._lock:
            settings_kv = self._settings_kv
            prepare_event = self._prepare_event
        if settings_kv is None or prepare_event is None:
            raise RuntimeError("DDS tagged exposure controls are not connected yet.")

        for key, value in settings.get_dict().items():
            payload = segment_bytes.encode([key.encode("utf-8"), str(value).encode("utf-8")])
            awaiter = settings_kv.try_set(payload, client.KVP_RET_AWAIT)
            if awaiter is None:
                raise RuntimeError(f"Failed to send exposure setting {key!r}.")
            try:
                result, state, reason = wait_for(awaiter, timeout=self._read_timeout)
            except TimeoutError as exc:
                raise TimeoutError(f"Timed out while writing exposure setting {key!r}.") from exc
            if state is not None:
                raise RuntimeError(
                    self._response_text(reason, f"Exposure setting {key!r} was rejected.")
                )
            if isinstance(result, bytes) and result not in (b"", magics.OP_OK):
                raise RuntimeError(self._response_text(result, f"Exposure setting {key!r} failed."))

        tags = dict(run_tags or {})
        tags["batch_uuid"] = str(batch_uuid)
        prepare_payload = encode_prepare_run_tags(tags)
        return self._call_controller_event(
            prepare_event,
            prepare_payload,
            self._start_timeout,
            on_feedback=getattr(self, "_on_start_progress", None),
        )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        if self._owns_client and self._client is not None:
            self._client.close()


@dataclass(frozen=True)
class BatchCoordinatorConfig:
    assessment: AssessmentConfig = AssessmentConfig()
    poll_interval: float = 10.0
    repair_attempt_limit: int = 3
    source_failure_limit: int = 3

    def __post_init__(self) -> None:
        if not _is_finite_number(self.poll_interval) or self.poll_interval <= 0:
            raise ValueError("Poll interval must be a finite positive number.")
        if self.repair_attempt_limit <= 0 or self.source_failure_limit <= 0:
            raise ValueError("Retry limits must be positive integers.")


def _assessment_fingerprint(assessment: BatchAssessment) -> tuple:
    decision = assessment.decision
    settings = tuple(sorted(decision.settings.get_dict().items())) if decision.settings is not None else None
    progress = tuple(
        (
            item.sample,
            item.cumulative_dose,
            item.cumulative_runtime,
            item.attempt_count,
            item.state,
        )
        for item in assessment.progress
    )
    return (
        decision.kind,
        decision.message,
        decision.run_uuid,
        decision.sample,
        settings,
        progress,
    )


def format_assessment(batch_uuid: uuid.UUID, assessment: BatchAssessment) -> str:
    decision = assessment.decision
    lines = [f"Batch {batch_uuid}: {decision.kind.value}: {decision.message}"]
    for item in assessment.progress:
        unit = "mJ/cm2" if item.mode is TargetMode.DOSE else "s"
        discrepancy = ""
        if item.state is SampleProgressState.OVERSHOT:
            discrepancy = f", overshoot={item.overshoot:.6g} {unit}"
        lines.append(
            f"  sample {item.sample + 1} (stored {item.sample}): {item.state.value}, "
            f"actual={item.actual:.6g}/{item.target:.6g} {unit}, attempts={item.attempt_count}, "
            f"dose={item.cumulative_dose:.6g} mJ/cm2, runtime={item.cumulative_runtime:.6g}s{discrepancy}"
        )
    if decision.settings is not None:
        lines.append(f"  proposed settings: {decision.settings.get_dict()}")
    return "\n".join(lines)


class BatchCoordinator:
    def __init__(
        self,
        plan: BatchPlan,
        batch_uuid: uuid.UUID,
        history: BatchHistorySource,
        controller: ControllerStateSource,
        *,
        config: BatchCoordinatorConfig = BatchCoordinatorConfig(),
        output: Callable[[str], None] = print,
        wall_clock: Callable[[], float] = time.time,
        acknowledged_runs: Iterable[uuid.UUID] = (),
        manually_paused: bool = False,
        owns_sources: bool = True,
    ) -> None:
        self.plan = plan
        self.batch_uuid = batch_uuid
        self.history = history
        self.controller = controller
        self.config = config
        self._output = output
        self._wall_clock = wall_clock
        self._owns_sources = owns_sources
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._manual_pause = manually_paused
        self._acknowledged_runs: set[uuid.UUID] = set(acknowledged_runs)
        self._repair_attempts: dict[uuid.UUID, int] = {}
        self._blocked_repair_runs: set[uuid.UUID] = set()
        self._source_failures = 0
        self._source_error_paused = False
        self._source_error_message = ""
        self._logic_error_paused = False
        self._logic_error_message = ""
        self._last_assessment: BatchAssessment | None = None
        self._last_fingerprint: tuple | None = None

    @property
    def last_assessment(self) -> BatchAssessment | None:
        with self._lock:
            return self._last_assessment

    @property
    def repair_attempts(self) -> dict[uuid.UUID, int]:
        with self._lock:
            return dict(self._repair_attempts)

    @property
    def acknowledged_runs(self) -> frozenset[uuid.UUID]:
        with self._lock:
            return frozenset(self._acknowledged_runs)

    def update_plan(self, plan: BatchPlan) -> None:
        with self._lock:
            assessment = self._last_assessment
            if assessment is not None and assessment.decision.kind is BatchDecisionKind.WAIT_ACTIVE:
                raise RuntimeError("Cannot revise a batch plan while its exposure is active.")
            self.plan = plan
            self._last_assessment = None
            self._last_fingerprint = None

    def _source_failure_assessment(self, message: str) -> BatchAssessment:
        progress = self._last_assessment.progress if self._last_assessment is not None else _progress_for(
            self.plan,
            (),
            self.config.assessment,
        )
        kind = (
            BatchDecisionKind.PAUSE_ERROR
            if self._source_failures >= self.config.source_failure_limit
            else BatchDecisionKind.WAIT_CONTROLLER
        )
        return _decision(
            progress,
            kind,
            f"Source refresh failed ({self._source_failures}/{self.config.source_failure_limit}): {message}",
        )

    def _publish(self, assessment: BatchAssessment, *, force: bool = False) -> None:
        fingerprint = _assessment_fingerprint(assessment)
        self._last_assessment = assessment
        if force or fingerprint != self._last_fingerprint:
            self._last_fingerprint = fingerprint
            self._output(format_assessment(self.batch_uuid, assessment))

    def poll_once(self) -> BatchAssessment:
        with self._lock:
            try:
                attempts = self.history.load_attempts(self.batch_uuid)
                controller = self.controller.snapshot()
                if not controller.available:
                    raise RuntimeError(controller.error or "Exposure-controller state is unavailable.")
            except Exception as exc:
                self._source_failures += 1
                if self._source_failures >= self.config.source_failure_limit:
                    self._source_error_paused = True
                    self._source_error_message = f"{type(exc).__name__}: {exc}"
                assessment = self._source_failure_assessment(f"{type(exc).__name__}: {exc}")
                self._publish(assessment)
                return assessment

            self._source_failures = 0
            repaired_this_poll: set[uuid.UUID] = set()
            while True:
                assessment = assess_batch(
                    self.plan,
                    attempts,
                    controller,
                    now=self._wall_clock(),
                    acknowledged_runs=self._acknowledged_runs,
                    blocked_repair_runs=self._blocked_repair_runs,
                    manually_paused=self._manual_pause or self._source_error_paused or self._logic_error_paused,
                    config=self.config.assessment,
                )
                decision = assessment.decision
                if decision.kind is not BatchDecisionKind.REPAIR_METRICS or decision.run_uuid is None:
                    break
                run_uuid = decision.run_uuid
                if run_uuid in repaired_this_poll:
                    break
                repaired_this_poll.add(run_uuid)
                self._repair_attempts[run_uuid] = self._repair_attempts.get(run_uuid, 0) + 1
                attempt_number = self._repair_attempts[run_uuid]
                try:
                    dose, runtime = self.history.repair_metrics(run_uuid)
                    self._output(
                        f"Batch {self.batch_uuid}: repaired run {run_uuid} "
                        f"(attempt {attempt_number}/{self.config.repair_attempt_limit}): "
                        f"dose={dose:.6g} mJ/cm2, runtime={runtime:.6g}s"
                    )
                    attempts = self.history.load_attempts(self.batch_uuid)
                    repaired_attempt = next(
                        (attempt for attempt in attempts if attempt.run_uuid == run_uuid),
                        None,
                    )
                    if repaired_attempt is not None and not _metrics_need_repair(repaired_attempt):
                        self._repair_attempts.pop(run_uuid, None)
                except Exception as exc:
                    self._output(
                        f"Batch {self.batch_uuid}: repair run {run_uuid} failed "
                        f"({attempt_number}/{self.config.repair_attempt_limit}): {type(exc).__name__}: {exc}"
                    )
                if self._repair_attempts.get(run_uuid, 0) >= self.config.repair_attempt_limit:
                    self._blocked_repair_runs.add(run_uuid)
                    assessment = assess_batch(
                        self.plan,
                        attempts,
                        controller,
                        now=self._wall_clock(),
                        acknowledged_runs=self._acknowledged_runs,
                        blocked_repair_runs=self._blocked_repair_runs,
                        manually_paused=True,
                        config=self.config.assessment,
                    )
                    break

            if assessment.decision.kind is BatchDecisionKind.PAUSE_ERROR:
                self._logic_error_paused = True
                self._logic_error_message = assessment.decision.message
            elif self._logic_error_paused and assessment.decision.kind in (
                BatchDecisionKind.PAUSED,
                BatchDecisionKind.START_REMAINDER,
                BatchDecisionKind.COMPLETE,
            ):
                assessment = _decision(
                    assessment.progress,
                    BatchDecisionKind.PAUSE_ERROR,
                    f"Planner error remains paused until resume: {self._logic_error_message}",
                )
            if self._source_error_paused and assessment.decision.kind in (
                BatchDecisionKind.PAUSED,
                BatchDecisionKind.START_REMAINDER,
                BatchDecisionKind.COMPLETE,
            ):
                assessment = _decision(
                    assessment.progress,
                    BatchDecisionKind.PAUSE_ERROR,
                    f"Source error remains paused until resume: {self._source_error_message}",
                )
            self._publish(assessment)
            return assessment

    def pause(self) -> None:
        with self._lock:
            self._manual_pause = True
            self._last_fingerprint = None

    def resume(self) -> None:
        with self._lock:
            decision = self._last_assessment.decision if self._last_assessment is not None else None
            if decision is not None and decision.kind is BatchDecisionKind.PAUSE_FAILURE and decision.run_uuid is not None:
                self._acknowledged_runs.add(decision.run_uuid)
            self._manual_pause = False
            self._source_error_paused = False
            self._source_error_message = ""
            self._logic_error_paused = False
            self._logic_error_message = ""
            self._source_failures = 0
            self._repair_attempts.clear()
            self._blocked_repair_runs.clear()
            self._last_fingerprint = None

    def status(self) -> str:
        with self._lock:
            if self._last_assessment is None:
                return f"Batch {self.batch_uuid}: no poll has completed yet."
            text = format_assessment(self.batch_uuid, self._last_assessment)
            if self._repair_attempts:
                retries = ", ".join(f"{run_uuid}={count}" for run_uuid, count in self._repair_attempts.items())
                text += f"\n  repair attempts: {retries}"
            return text

    def start_next(
        self,
        *,
        run_tags: Mapping[str, str | int | float] | None = None,
    ) -> tuple[bool, str]:
        with self._lock:
            assessment = self.poll_once()
            decision = assessment.decision
            if decision.kind is not BatchDecisionKind.START_REMAINDER or decision.settings is None:
                message = f"Cannot start: {decision.kind.value}: {decision.message}"
                self._output(f"Batch {self.batch_uuid}: {message}")
                return False, message
            try:
                result = self.controller.start_exposure(
                    decision.settings,
                    self.batch_uuid,
                    run_tags=run_tags,
                )
            except Exception as exc:
                message = f"Tagged exposure start failed: {type(exc).__name__}: {exc}"
                self._output(f"Batch {self.batch_uuid}: {message}")
                return False, message

            message = (
                f"Started sample {decision.sample + 1 if decision.sample is not None else '?'} "
                f"with batch tag {self.batch_uuid}: {result}"
            )
            self._last_fingerprint = None
            self._output(f"Batch {self.batch_uuid}: {message}")
            return True, message

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="batch-coordinator", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(self.config.poll_interval)

    def close(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=self.config.poll_interval + 5.0)
            if thread.is_alive():
                raise TimeoutError("Timed out while closing the batch coordinator.")
        if self._owns_sources:
            self.controller.close()
            self.history.close()

