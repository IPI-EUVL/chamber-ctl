from __future__ import annotations

import uuid

import pytest

from chamber_ctl.subsystems.batcher import (
    BatchManifest,
    BatchPlan,
    BatchPlanEntry,
    ControlGenerator,
    ExecutionMode,
    ExposureTemplate,
    LinearContrastDoseGenerator,
    ManifestStatus,
    TargetAssignmentGenerator,
    TargetMode,
    apply_plan_generator,
    decode_batch_manifest,
    encode_batch_manifest,
)


def _plan(entries=()) -> BatchPlan:
    return BatchPlan(ExposureTemplate("Contrast A", operator="Operator"), tuple(entries))


def test_linear_contrast_generator_preserves_explicit_selection_order() -> None:
    application = apply_plan_generator(
        _plan(),
        LinearContrastDoseGenerator(10.0, 40.0),
        (4, 1, 7, 2),
    )

    assert application.samples == (4, 1, 7, 2)
    assert [(entry.sample, entry.target) for entry in application.plan.entries] == [
        (4, 10.0),
        (1, 20.0),
        (7, 30.0),
        (2, 40.0),
    ]


def test_generators_replace_selected_entries_with_ordinary_plan_entries() -> None:
    original = _plan((BatchPlanEntry(0, TargetMode.DOSE, 5.0), BatchPlanEntry(1, TargetMode.TIME, 30.0)))

    application = apply_plan_generator(
        original,
        TargetAssignmentGenerator(TargetMode.DOSE, 25.0),
        (1, 3),
    )

    assert [(entry.sample, entry.mode, entry.target) for entry in application.plan.entries] == [
        (0, TargetMode.DOSE, 5.0),
        (1, TargetMode.DOSE, 25.0),
        (3, TargetMode.DOSE, 25.0),
    ]


def test_control_generator_creates_explicit_zero_dose_controls() -> None:
    application = apply_plan_generator(_plan(), ControlGenerator(), (6,))

    assert application.plan.entries[0].is_control is True
    assert application.plan.entries[0].target == 0.0
    with pytest.raises(ValueError, match="time targets"):
        BatchPlanEntry(6, TargetMode.TIME, 0.0)


def test_contrast_generator_rejects_implicit_controls_and_ambiguous_single_sample_range() -> None:
    with pytest.raises(ValueError, match="positive"):
        LinearContrastDoseGenerator(0.0, 10.0).generate((0, 1))
    with pytest.raises(ValueError, match="equal"):
        LinearContrastDoseGenerator(10.0, 20.0).generate((0,))


def test_manifest_round_trip_preserves_plan_control_and_audit_state() -> None:
    batch_uuid = uuid.uuid4()
    acknowledged = uuid.uuid4()
    manifest = BatchManifest(
        batch_uuid=batch_uuid,
        revision=3,
        status=ManifestStatus.ACTIVE,
        mode=ExecutionMode.MANUAL,
        plan=_plan((BatchPlanEntry(0, TargetMode.DOSE, 0.0), BatchPlanEntry(1, TargetMode.DOSE, 20.0))),
        created_at=100.0,
        updated_at=120.0,
        origin="web_work_order",
        submitted_by="pi@example.edu",
        paused=True,
        acknowledged_runs=(acknowledged,),
        revision_note="Retargeted sample 2",
    )

    restored = decode_batch_manifest(encode_batch_manifest(manifest))

    assert restored == manifest