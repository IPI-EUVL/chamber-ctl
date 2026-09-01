import json
import threading
import uuid
import runpy
from pathlib import Path

import pytest

from chamber_ctl.data.calibration import SourceCalibrationBinding
from chamber_ctl.subsystems.batcher import (
    BatchCoordinator,
    BatchCoordinatorConfig,
    BatchDecisionKind,
    BatchHistoryStore,
    BatchPlan,
    BatchPlanEntry,
    ControllerSnapshot,
    DdsControllerStateSource,
    ExposureAttempt,
    ExposureTemplate,
    SampleProgressState,
    TargetAssignmentGenerator,
    TargetMode,
    _attempt_from_record,
    apply_plan_generator,
    assess_batch,
    batch_plan_from_dict,
    batch_plan_to_dict,
)
from chamber_ctl.subsystems.exposure_controller import ExposureSettings


def test_exposure_settings_instances_do_not_share_data() -> None:
    first = ExposureSettings(target_dose=10.0, sample="0")
    second = ExposureSettings(target_time=20.0, sample="1")

    first.set_attr("name", "First")

    assert first.get_dict()["name"] == "First"
    assert first.get_target_dose() == 10.0
    assert first.get_sample() == "0"
    assert second.get_dict()["name"] == ""
    assert second.get_target_dose() == 0.0
    assert second.get_target_time() == 20.0
    assert second.get_sample() == "1"


def _plan(*entries: BatchPlanEntry) -> BatchPlan:
    return BatchPlan(
        ExposureTemplate(
            name="Batch A",
            description="Manual planner test",
            operator="Operator",
            zr_filter="ZR-1",
            sample_type="resist",
            base_pressure=1.0,
            operating_pressure=2.0,
            flow_sccm=3.0,
        ),
        tuple(entries) or (BatchPlanEntry(0, TargetMode.DOSE, 10.0),),
    )


def _attempt(
    *,
    sample: int | None = 0,
    dose: float | None = 10.0,
    runtime: float | None = 20.0,
    status: str | None = "STOPPED",
    created_at: float = 10.0,
    end_time: float | None = 20.0,
    snapshots: int = 1,
) -> ExposureAttempt:
    return ExposureAttempt(
        run_uuid=uuid.uuid4(),
        sample=sample,
        created_at=created_at,
        end_time=end_time,
        status=status,
        dose=dose,
        runtime=runtime,
        snapshot_count=snapshots,
    )


def test_plan_rejects_duplicate_samples_and_invalid_targets() -> None:
    with pytest.raises(ValueError, match="one entry per sample"):
        _plan(BatchPlanEntry(0, TargetMode.DOSE, 10.0), BatchPlanEntry(0, TargetMode.TIME, 20.0))
    with pytest.raises(ValueError, match="time targets"):
        BatchPlanEntry(0, TargetMode.DOSE, -1.0)
    with pytest.raises(ValueError, match="time targets"):
        BatchPlanEntry(0, TargetMode.TIME, 0.0)
    with pytest.raises(ValueError, match="zero-based"):
        BatchPlanEntry(12, TargetMode.DOSE, 10.0)


def test_empty_history_starts_first_sample_with_independent_full_settings() -> None:
    plan = _plan(
        BatchPlanEntry(2, TargetMode.DOSE, 15.0),
        BatchPlanEntry(4, TargetMode.TIME, 30.0),
    )

    first = assess_batch(plan, (), ControllerSnapshot.stopped(), now=100.0)
    second = assess_batch(plan, (), ControllerSnapshot.stopped(), now=100.0)

    assert first.decision.kind is BatchDecisionKind.START_REMAINDER
    assert first.decision.sample == 2
    assert first.decision.settings.get_dict() == {
        "name": "Batch A",
        "description": "Manual planner test",
        "target_time": 0.0,
        "target_dose": 15.0,
        "operator": "Operator",
        "zr_filter": "ZR-1",
        "sample": "2",
        "sample_type": "resist",
        "base_pressure": 1.0,
        "operating_pressure": 2.0,
        "flow_sccm": 3.0,
        "calibration_profile_id": "",
        "calibration_revision": 0,
        "chopper_frequency_hz": 192.0,
        "source_calibrations": [],
    }
    assert second.decision.settings is not first.decision.settings


def test_sample_overrides_round_trip_merge_into_settings_and_survive_target_regeneration() -> None:
    plan = _plan(
        BatchPlanEntry(
            2,
            TargetMode.DOSE,
            15.0,
            overrides={"operator": "Override operator", "flow_sccm": 4.5},
        ),
    )

    encoded = batch_plan_to_dict(plan)
    restored = batch_plan_from_dict(encoded)
    settings = restored.template.settings_for(restored.entries[0], 10.0)
    regenerated = apply_plan_generator(
        restored,
        TargetAssignmentGenerator(TargetMode.DOSE, 20.0),
        (2,),
    )

    assert encoded["schema_version"] == 4
    assert dict(restored.entries[0].overrides) == {"operator": "Override operator", "flow_sccm": 4.5}
    assert settings.get_operator() == "Override operator"
    assert settings.get_flow_sccm() == pytest.approx(4.5)
    assert settings.get_zr_filter() == "ZR-1"
    assert regenerated.plan.entries[0].target == pytest.approx(20.0)
    assert dict(regenerated.plan.entries[0].overrides) == dict(restored.entries[0].overrides)


def test_legacy_plan_entries_load_without_overrides() -> None:
    encoded = batch_plan_to_dict(_plan(BatchPlanEntry(2, TargetMode.DOSE, 15.0)))
    encoded["schema_version"] = 1
    encoded["template"].pop("calibration_profile_id")
    encoded["template"].pop("calibration_revision")
    encoded["template"].pop("chopper_frequency_hz")
    encoded["template"].pop("source_calibrations")
    encoded["entries"][0].pop("overrides")

    restored = batch_plan_from_dict(encoded)

    assert dict(restored.entries[0].overrides) == {}


def test_schema_two_plan_entries_preserve_overrides_without_calibration_fields() -> None:
    encoded = batch_plan_to_dict(
        _plan(BatchPlanEntry(2, TargetMode.DOSE, 15.0, overrides={"operator": "Historical operator"}))
    )
    encoded["schema_version"] = 2
    encoded["template"].pop("calibration_profile_id")
    encoded["template"].pop("calibration_revision")
    encoded["template"].pop("chopper_frequency_hz")
    encoded["template"].pop("source_calibrations")

    restored = batch_plan_from_dict(encoded)

    assert dict(restored.entries[0].overrides) == {"operator": "Historical operator"}
    assert restored.template.calibration_profile_id == ""
    assert restored.template.calibration_revision == 0
    assert restored.template.chopper_frequency_hz is None


def test_schema_three_plan_loads_without_source_calibrations() -> None:
    encoded = batch_plan_to_dict(_plan())
    encoded["schema_version"] = 3
    encoded["template"].pop("source_calibrations")

    restored = batch_plan_from_dict(encoded)

    assert restored.template.source_calibrations == ()


def test_batch_plan_round_trips_source_calibration_bindings() -> None:
    binding = SourceCalibrationBinding("siglent", "scope-1", uuid.uuid4(), 2)
    plan = BatchPlan(
        ExposureTemplate("Source calibrated", source_calibrations=(binding,)),
        (BatchPlanEntry(0, TargetMode.DOSE, 10.0),),
    )

    restored = batch_plan_from_dict(batch_plan_to_dict(plan))
    settings = restored.template.settings_for(restored.entries[0], 5.0)

    assert restored.template.source_calibrations == (binding,)
    assert settings.get_source_calibrations() == (binding,)


def test_finalization_and_metric_repair_precede_tallying() -> None:
    recent = _attempt(end_time=98.0, dose=None, runtime=None)
    waiting = assess_batch(_plan(), (recent,), ControllerSnapshot.stopped(), now=100.0)
    repair = assess_batch(_plan(), (recent,), ControllerSnapshot.stopped(), now=104.0)

    assert waiting.decision.kind is BatchDecisionKind.WAIT_FINALIZATION
    assert repair.decision.kind is BatchDecisionKind.REPAIR_METRICS
    assert repair.decision.run_uuid == recent.run_uuid


def test_zero_metrics_are_valid_only_without_snapshots() -> None:
    empty = _attempt(dose=0.0, runtime=0.0, snapshots=0)
    populated = _attempt(dose=0.0, runtime=0.0, snapshots=1)

    no_snapshots = assess_batch(_plan(), (empty,), ControllerSnapshot.stopped(), now=100.0)
    with_snapshots = assess_batch(_plan(), (populated,), ControllerSnapshot.stopped(), now=100.0)

    assert no_snapshots.decision.kind is BatchDecisionKind.PAUSE_FAILURE
    assert with_snapshots.decision.kind is BatchDecisionKind.REPAIR_METRICS


def test_cumulative_actuals_set_remainder_and_overshoot_status() -> None:
    plan = _plan(
        BatchPlanEntry(0, TargetMode.DOSE, 15.0),
        BatchPlanEntry(1, TargetMode.TIME, 20.0),
    )
    first = _attempt(sample=0, dose=5.0, runtime=2.0, status="ABORTED", created_at=1.0, end_time=2.0)
    second = _attempt(sample=0, dose=4.0, runtime=2.0, created_at=3.0, end_time=4.0)
    time_attempt = _attempt(sample=1, dose=1.0, runtime=26.0, created_at=5.0, end_time=6.0)

    assessment = assess_batch(
        plan,
        (first, second, time_attempt),
        ControllerSnapshot.stopped(),
        now=100.0,
        acknowledged_runs={second.run_uuid, time_attempt.run_uuid},
    )

    assert assessment.decision.kind is BatchDecisionKind.START_REMAINDER
    assert assessment.decision.settings.get_target_dose() == pytest.approx(6.0)
    assert assessment.progress[0].cumulative_dose == pytest.approx(9.0)
    assert assessment.progress[1].state is SampleProgressState.OVERSHOT
    assert assessment.progress[1].overshoot == pytest.approx(6.0)


def test_tolerance_boundaries_are_complete() -> None:
    lower = _attempt(dose=9.0)
    upper = _attempt(dose=11.0)

    at_lower = assess_batch(_plan(), (lower,), ControllerSnapshot.stopped(), now=100.0)
    at_upper = assess_batch(_plan(), (upper,), ControllerSnapshot.stopped(), now=100.0)

    assert at_lower.progress[0].state is SampleProgressState.WITHIN_TOLERANCE
    assert at_lower.decision.kind is BatchDecisionKind.COMPLETE
    assert at_upper.progress[0].state is SampleProgressState.WITHIN_TOLERANCE
    assert at_upper.decision.kind is BatchDecisionKind.COMPLETE


def test_abort_and_clean_shortfall_pause_until_that_run_is_acknowledged() -> None:
    aborted = _attempt(dose=12.0, status="ABORTED")
    stopped = _attempt(dose=5.0)

    abort_pause = assess_batch(_plan(), (aborted,), ControllerSnapshot.stopped(), now=100.0)
    abort_done = assess_batch(
        _plan(),
        (aborted,),
        ControllerSnapshot.stopped(),
        now=100.0,
        acknowledged_runs={aborted.run_uuid},
    )
    stopped_pause = assess_batch(_plan(), (stopped,), ControllerSnapshot.stopped(), now=100.0)
    stopped_start = assess_batch(
        _plan(),
        (stopped,),
        ControllerSnapshot.stopped(),
        now=100.0,
        acknowledged_runs={stopped.run_uuid},
    )

    assert abort_pause.decision.kind is BatchDecisionKind.PAUSE_FAILURE
    assert abort_done.decision.kind is BatchDecisionKind.COMPLETE
    assert stopped_pause.decision.kind is BatchDecisionKind.PAUSE_FAILURE
    assert stopped_start.decision.kind is BatchDecisionKind.START_REMAINDER
    assert stopped_start.decision.settings.get_target_dose() == pytest.approx(5.0)


def test_allowed_negative_values_count_but_large_negatives_error_pause() -> None:
    allowed = _attempt(dose=-0.05, runtime=-0.05, snapshots=0)
    invalid = _attempt(dose=-0.11, runtime=1.0)

    counted = assess_batch(
        _plan(),
        (allowed,),
        ControllerSnapshot.stopped(),
        now=100.0,
        acknowledged_runs={allowed.run_uuid},
    )
    rejected = assess_batch(_plan(), (invalid,), ControllerSnapshot.stopped(), now=100.0)

    assert counted.progress[0].cumulative_dose == pytest.approx(-0.05)
    assert counted.decision.settings.get_target_dose() == pytest.approx(10.05)
    assert rejected.decision.kind is BatchDecisionKind.PAUSE_ERROR


def test_active_run_must_match_controller_and_unknown_samples_error_pause() -> None:
    active = _attempt(status=None, end_time=None, dose=None, runtime=None)
    matching = assess_batch(_plan(), (active,), ControllerSnapshot.active(active.run_uuid), now=100.0)
    orphaned = assess_batch(_plan(), (active,), ControllerSnapshot.stopped(), now=100.0)
    unknown = _attempt(sample=9)
    bad_sample = assess_batch(_plan(), (unknown,), ControllerSnapshot.stopped(), now=100.0)

    assert matching.decision.kind is BatchDecisionKind.WAIT_ACTIVE
    assert orphaned.decision.kind is BatchDecisionKind.PAUSE_ERROR
    assert bad_sample.decision.kind is BatchDecisionKind.PAUSE_ERROR


def test_active_run_preserves_finalized_history_progress() -> None:
    plan = _plan(
        BatchPlanEntry(0, TargetMode.DOSE, 5.0),
        BatchPlanEntry(1, TargetMode.DOSE, 10.0),
    )
    completed = _attempt(sample=0, dose=5.0, runtime=2.0, created_at=1.0, end_time=2.0)
    active = _attempt(sample=1, dose=None, runtime=None, status=None, created_at=3.0, end_time=None)

    assessment = assess_batch(plan, (completed, active), ControllerSnapshot.active(active.run_uuid), now=100.0)

    assert assessment.decision.kind is BatchDecisionKind.WAIT_ACTIVE
    assert assessment.progress[0].state is SampleProgressState.WITHIN_TOLERANCE
    assert assessment.progress[0].cumulative_dose == pytest.approx(5.0)
    assert assessment.progress[1].attempt_count == 0


def test_busy_controller_and_manual_pause_block_only_start_intents() -> None:
    busy = assess_batch(_plan(), (), ControllerSnapshot.active(uuid.uuid4()), now=100.0)
    paused = assess_batch(_plan(), (), ControllerSnapshot.stopped(), now=100.0, manually_paused=True)
    repair = _attempt(dose=None, runtime=None)
    paused_repair = assess_batch(
        _plan(),
        (repair,),
        ControllerSnapshot.stopped(),
        now=100.0,
        manually_paused=True,
    )

    assert busy.decision.kind is BatchDecisionKind.WAIT_CONTROLLER
    assert paused.decision.kind is BatchDecisionKind.PAUSED
    assert paused_repair.decision.kind is BatchDecisionKind.REPAIR_METRICS


def test_blocked_repair_and_unknown_status_error_pause() -> None:
    missing = _attempt(dose=None, runtime=None)
    blocked = assess_batch(
        _plan(),
        (missing,),
        ControllerSnapshot.stopped(),
        now=100.0,
        blocked_repair_runs={missing.run_uuid},
    )
    unknown = _attempt(status="BROKEN")
    bad_status = assess_batch(_plan(), (unknown,), ControllerSnapshot.stopped(), now=100.0)

    assert blocked.decision.kind is BatchDecisionKind.PAUSE_ERROR
    assert bad_status.decision.kind is BatchDecisionKind.PAUSE_ERROR


class _FakeHistory:
    def __init__(self, attempts=(), *, load_errors=0, repair_error: Exception | None = None):
        self.attempts = tuple(attempts)
        self.load_errors = load_errors
        self.repair_error = repair_error
        self.load_count = 0
        self.repair_count = 0
        self.closed = False

    def load_attempts(self, _batch_uuid):
        self.load_count += 1
        if self.load_count <= self.load_errors:
            raise OSError("history unavailable")
        return self.attempts

    def repair_metrics(self, run_uuid):
        self.repair_count += 1
        if self.repair_error is not None:
            raise self.repair_error
        repaired = []
        for attempt in self.attempts:
            if attempt.run_uuid == run_uuid:
                attempt = ExposureAttempt(
                    run_uuid=attempt.run_uuid,
                    sample=attempt.sample,
                    created_at=attempt.created_at,
                    end_time=attempt.end_time,
                    status=attempt.status,
                    end_reason=attempt.end_reason,
                    dose=5.0,
                    runtime=2.0,
                    snapshot_count=attempt.snapshot_count,
                )
            repaired.append(attempt)
        self.attempts = tuple(repaired)
        return 5.0, 2.0

    def close(self):
        self.closed = True


class _FakeController:
    def __init__(self, snapshot=None, *, start_error=None):
        self.value = snapshot or ControllerSnapshot.stopped()
        self.start_error = start_error
        self.starts = []
        self.closed = False

    def snapshot(self):
        return self.value

    def acquire_automation(self, owner_name):
        return f"Acquired by {owner_name}"

    def release_automation(self):
        return "Released"

    def start_exposure(self, settings, batch_uuid, run_tags=None):
        if self.start_error is not None:
            raise self.start_error
        self.starts.append((settings, batch_uuid, run_tags))
        return "Run successfully started"

    def close(self):
        self.closed = True


def _coordinator(history, *, output=None, source_failure_limit=3, controller=None):
    return BatchCoordinator(
        _plan(),
        uuid.uuid4(),
        history,
        controller or _FakeController(),
        config=BatchCoordinatorConfig(
            poll_interval=10.0,
            repair_attempt_limit=3,
            source_failure_limit=source_failure_limit,
        ),
        output=(output or (lambda _message: None)),
        wall_clock=lambda: 100.0,
    )


def test_coordinator_repairs_both_metrics_then_surfaces_failure_pause() -> None:
    missing = _attempt(dose=None, runtime=None)
    history = _FakeHistory((missing,))
    coordinator = _coordinator(history)

    assessment = coordinator.poll_once()

    assert history.repair_count == 1
    assert coordinator.repair_attempts == {}
    assert assessment.decision.kind is BatchDecisionKind.PAUSE_FAILURE
    assert assessment.progress[0].cumulative_dose == pytest.approx(5.0)


def test_coordinator_blocks_after_three_repair_polls_and_resume_resets() -> None:
    missing = _attempt(dose=None, runtime=None)
    history = _FakeHistory((missing,), repair_error=OSError("snapshot read failed"))
    coordinator = _coordinator(history)

    assert coordinator.poll_once().decision.kind is BatchDecisionKind.REPAIR_METRICS
    assert coordinator.poll_once().decision.kind is BatchDecisionKind.REPAIR_METRICS
    third = coordinator.poll_once()

    assert third.decision.kind is BatchDecisionKind.PAUSE_ERROR
    assert history.repair_count == 3
    coordinator.resume()
    assert coordinator.poll_once().decision.kind is BatchDecisionKind.REPAIR_METRICS
    assert history.repair_count == 4


def test_coordinator_source_failures_pause_on_third_poll_and_latch_until_resume() -> None:
    history = _FakeHistory(load_errors=3)
    coordinator = _coordinator(history)

    assert coordinator.poll_once().decision.kind is BatchDecisionKind.WAIT_CONTROLLER
    assert coordinator.poll_once().decision.kind is BatchDecisionKind.WAIT_CONTROLLER
    assert coordinator.poll_once().decision.kind is BatchDecisionKind.PAUSE_ERROR
    recovered_but_paused = coordinator.poll_once()

    assert recovered_but_paused.decision.kind is BatchDecisionKind.PAUSE_ERROR
    coordinator.resume()
    assert coordinator.poll_once().decision.kind is BatchDecisionKind.START_REMAINDER


def test_coordinator_logic_errors_remain_paused_after_data_is_fixed_until_resume() -> None:
    invalid = _attempt(sample=9)
    history = _FakeHistory((invalid,))
    coordinator = _coordinator(history)

    assert coordinator.poll_once().decision.kind is BatchDecisionKind.PAUSE_ERROR
    history.attempts = (_attempt(dose=10.0),)
    assert coordinator.poll_once().decision.kind is BatchDecisionKind.PAUSE_ERROR

    coordinator.resume()

    assert coordinator.poll_once().decision.kind is BatchDecisionKind.COMPLETE


def test_coordinator_resume_acknowledges_only_current_failure() -> None:
    short = _attempt(dose=5.0)
    history = _FakeHistory((short,))
    coordinator = _coordinator(history)

    assert coordinator.poll_once().decision.kind is BatchDecisionKind.PAUSE_FAILURE
    coordinator.resume()
    resumed = coordinator.poll_once()

    assert resumed.decision.kind is BatchDecisionKind.START_REMAINDER
    assert resumed.decision.run_uuid is None


def test_coordinator_deduplicates_automatic_output_but_status_is_complete() -> None:
    output = []
    coordinator = _coordinator(_FakeHistory(), output=output.append)

    coordinator.poll_once()
    coordinator.poll_once()

    assert len(output) == 1
    assert "start_remainder" in coordinator.status()
    assert "sample 1" in coordinator.status()


def test_coordinator_start_command_uses_fresh_plan_settings_and_exact_batch_uuid() -> None:
    controller = _FakeController()
    coordinator = _coordinator(_FakeHistory(), controller=controller)

    ok, message = coordinator.start_next()

    assert ok is True
    assert "Started sample 1" in message
    assert len(controller.starts) == 1
    settings, batch_uuid, run_tags = controller.starts[0]
    assert batch_uuid == coordinator.batch_uuid
    assert settings.get_sample() == "0"
    assert settings.get_target_dose() == 10.0
    assert settings.get_target_time() == 0.0
    assert run_tags is None


def test_coordinator_start_command_refuses_while_paused() -> None:
    controller = _FakeController()
    coordinator = _coordinator(_FakeHistory(), controller=controller)
    coordinator.pause()

    ok, message = coordinator.start_next()

    assert ok is False
    assert "paused" in message
    assert controller.starts == []


class _FakeEntry:
    def __init__(self):
        self.resources = {
            "snap_00000000-0000-0000-0000-000000000001.npz": "snapshot",
            "snap_00000000-0000-0000-0000-000000000001.json": "snap_meta",
        }

    def get_timestamp(self):
        return 1.0

    def list_resources(self):
        return self.resources.items()


def test_history_counts_hdf5_euv_snapshot_resources() -> None:
    class _Hdf5Entry:
        def get_timestamp(self):
            return 1.0

        def list_resources(self):
            return [("snap_00000000-0000-0000-0000-000000000001.h5", "euv_snapshot")]

    class _Hdf5Record(_FakeRecord):
        def __init__(self, run_uuid):
            super().__init__(run_uuid)
            self.entry = _Hdf5Entry()

    assert _attempt_from_record(_Hdf5Record(uuid.uuid4())).snapshot_count == 1


class _FakeRecord:
    def __init__(self, run_uuid):
        self.tags = {
            "run": run_uuid.hex,
            "sample": "2",
            "dose": "3.5",
            "runtime": "4.5",
            "target_dose": "not-a-number",
            "target_time": object(),
        }
        self.entry = _FakeEntry()

    def get_tags(self):
        return self.tags

    def get_metadata(self):
        return {"created_at": 10.0}

    def get_end_metadata(self):
        return {"end_time": 20.0, "status": "STOPPED", "end_reason": "done"}

    def get_record(self):
        return self.entry


def test_history_normalization_never_reads_or_validates_stored_targets() -> None:
    run_uuid = uuid.uuid4()

    attempt = _attempt_from_record(_FakeRecord(run_uuid))

    assert attempt.run_uuid == run_uuid
    assert attempt.sample == 2
    assert attempt.dose == 3.5
    assert attempt.runtime == 4.5
    assert attempt.snapshot_count == 1
    assert attempt.validation_error is None


def test_manual_script_import_has_no_startup_side_effects() -> None:
    script = Path(__file__).parents[1] / "scripts" / "test_batcher.py"

    namespace = runpy.run_path(str(script), run_name="batcher_manual_import_test")

    assert callable(namespace["main"])


def _create_library_run(library, batch_uuid, *, sample="0"):
    from ipi_ecs.subsystems.experiment_controller import RunState

    run_uuid = uuid.uuid4()
    settings = ExposureSettings(target_dose=100.0, sample=sample)
    state = RunState("exposure", settings, s_uuid=run_uuid)
    entry = library.create_entry("Batch test", "Fixture")
    entry.set_tag("experiment", "exposure")
    entry.set_tag("run", run_uuid.hex)
    entry.set_tag("batch_uuid", str(batch_uuid))
    entry.set_tag("sample", sample)
    entry.set_tag("target_dose", "ignored-invalid-target")

    with entry.resource("run.json", "run_state", "w") as resource:
        resource.write(state.encode())
    with entry.resource("metadata.json", "metadata", "w") as resource:
        json.dump({"created_at": 10.0, "version": 1}, resource)
    with entry.resource("end_metadata.json", "metadata", "w") as resource:
        json.dump({"end_time": 20.0, "status": "STOPPED", "end_reason": "fixture"}, resource)
    return run_uuid


def test_history_store_filters_exact_batch_and_persists_valid_no_snapshot_zero(tmp_path) -> None:
    from ipi_ecs.db.db_library import Library

    batch_uuid = uuid.uuid4()
    writer = Library(str(tmp_path))
    matching_uuid = _create_library_run(writer, batch_uuid)
    _create_library_run(writer, uuid.uuid4(), sample="1")
    writer.close()

    store = BatchHistoryStore(str(tmp_path))
    try:
        attempts = store.load_attempts(batch_uuid)
        repaired = store.repair_metrics(matching_uuid)
        refreshed = store.load_attempts(batch_uuid)
    finally:
        store.close()

    assert [attempt.run_uuid for attempt in attempts] == [matching_uuid]
    assert attempts[0].dose is None
    assert attempts[0].runtime is None
    assert attempts[0].snapshot_count == 0
    assert repaired == (0.0, 0.0)
    assert refreshed[0].dose == 0.0
    assert refreshed[0].runtime == 0.0
    assert refreshed[0].validation_error is None


def test_dds_controller_state_payload_decodes_idle_and_active_runs() -> None:
    import segment_bytes
    from ipi_ecs.subsystems.experiment_controller import ExperimentController, RunState

    run_uuid = uuid.uuid4()
    run = RunState("exposure", ExposureSettings(target_dose=10.0, sample="0"), s_uuid=run_uuid)
    stopped_payload = segment_bytes.encode(
        [ExperimentController.RUN_STATE_STOPPED.to_bytes(1, "big"), b""]
    )
    active_payload = segment_bytes.encode(
        [ExperimentController.RUN_STATE_RUNNING.to_bytes(1, "big"), run.encode().encode("utf-8")]
    )

    stopped = DdsControllerStateSource._decode(stopped_payload)
    active = DdsControllerStateSource._decode(active_payload)

    assert stopped == ControllerSnapshot.stopped()
    assert active == ControllerSnapshot.active(run_uuid)


class _FakeSettingsKv:
    def __init__(self):
        self.writes = []

    def try_set(self, payload, _return_type):
        self.writes.append(payload)
        return object()


class _FakeStartEventHandle:
    def is_in_progress(self):
        return False

    def get_state(self, _target_uuid):
        import ipi_ecs.dds.client as client

        return client.EVENT_OK

    def get_result(self, _target_uuid):
        return b"Run successfully started with UUID: 00000000-0000-0000-0000-000000000001"


class _FakeStartEvent:
    def __init__(self):
        self.calls = []

    def call(self, payload, targets):
        self.calls.append((payload, targets))
        return _FakeStartEventHandle()


class _FeedbackStartEventHandle:
    def __init__(self):
        self._checks = 0

    def is_in_progress(self):
        self._checks += 1
        return self._checks == 1

    def get_last_update(self):
        return float(self._checks)

    def get_state(self, _target_uuid):
        import ipi_ecs.dds.client as client

        return client.EVENT_OK

    def get_result(self, _target_uuid):
        return b"Preinitiation started." if self._checks == 1 else b"Run successfully started."


class _FeedbackStartEvent:
    def call(self, _payload, _targets):
        return _FeedbackStartEventHandle()


def test_dds_start_writes_settings_and_calls_tagged_prepare_payload(monkeypatch) -> None:
    import segment_bytes
    import ipi_ecs.cli.captive_cli as captive_cli
    from ipi_ecs.subsystems.experiment_controller import decode_prepare_run_tags

    monkeypatch.setattr(captive_cli, "wait_for", lambda _awaiter, timeout: (b"", None, None))
    settings_kv = _FakeSettingsKv()
    prepare_event = _FakeStartEvent()
    source = object.__new__(DdsControllerStateSource)
    source._lock = threading.Lock()
    source._settings_kv = settings_kv
    source._prepare_event = prepare_event
    source._read_timeout = 5.0
    source._start_timeout = 60.0
    batch_uuid = uuid.uuid4()
    settings = _plan().template.settings_for(_plan().entries[0], 10.0)

    result = source.start_exposure(settings, batch_uuid)

    written_settings = {
        key.decode("utf-8"): value.decode("utf-8")
        for key, value in (segment_bytes.decode(payload) for payload in settings_kv.writes)
    }
    assert written_settings["sample"] == "0"
    assert written_settings["target_dose"] == "10.0"
    assert len(prepare_event.calls) == 1
    payload, targets = prepare_event.calls[0]
    assert decode_prepare_run_tags(payload) == {"batch_uuid": str(batch_uuid)}
    assert len(targets) == 1
    assert "Run successfully started" in result


def test_dds_start_forwards_prepare_feedback(monkeypatch) -> None:
    import chamber_ctl.subsystems.batcher as batcher

    monkeypatch.setattr(batcher.time, "sleep", lambda _seconds: None)
    feedback = []
    source = object.__new__(DdsControllerStateSource)

    result = source._call_controller_event(
        _FeedbackStartEvent(),
        b"",
        timeout=1.0,
        on_feedback=feedback.append,
    )

    assert result == "Run successfully started."
    assert feedback == [
        "Exposure start request sent; awaiting controller feedback.",
        "Preinitiation started.",
        "Run successfully started.",
    ]