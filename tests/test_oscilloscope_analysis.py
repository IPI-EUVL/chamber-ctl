from __future__ import annotations

import io
import json
import threading
import uuid

import numpy as np
import pytest
import segment_bytes

from ipi_ecs.subsystems.experiment_controller import ExperimentController, RunState

from chamber_ctl.subsystems.exposure_controller import ExposureSettings
from chamber_ctl.subsystems.oscilloscope import (
    OscilloscopeSubsystem,
    ParallelCalc,
    ScopeWriter,
    analyze_snapshot,
    analyze_snapshot_from_metadata,
    calculate_avg_pulsedose,
    calculate_dose_of_experiment,
    calculate_dose_raw,
    calculate_peak_volts,
)


def _waveform(pulse_count: int = 2, points_per_pulse: int = 40) -> tuple[np.ndarray, np.ndarray]:
    pulse = np.concatenate((np.zeros(25), np.full(points_per_pulse - 25, 3.0)))
    data = np.column_stack((np.arange(pulse_count * points_per_pulse, dtype=float) * 1e-9, np.tile(pulse, pulse_count)))
    indexes = np.column_stack((np.arange(pulse_count) * points_per_pulse, np.arange(pulse_count, dtype=float) * 10.0))
    return data, indexes


def test_analysis_preserves_legacy_two_pulse_values_without_mutating_input() -> None:
    data, indexes = _waveform()
    data_before = data.copy()

    analysis = analyze_snapshot(0, 1_000_000_000, data, indexes)

    assert np.array_equal(data, data_before)
    assert analysis.average_pulse_dose_mj_cm2 == pytest.approx(0.00012428571428571428)
    assert analysis.average_pulse_dose_mj_cm2.hex() == "0x1.04a56280c13cdp-13"
    assert analysis.is_step_exposure is False
    assert analysis.pulse_span_seconds == pytest.approx(10.0)
    assert analysis.effective_duration_seconds == pytest.approx(1.0)
    assert analysis.total_dose_mj_cm2 == pytest.approx(0.012428571428571428)
    assert analysis.total_dose_mj_cm2.hex() == "0x1.974269e92def0p-7"
    assert analysis.delivered_dose_rate_mj_cm2_s == pytest.approx(0.012428571428571428)
    assert calculate_avg_pulsedose(data, indexes) == pytest.approx((0.00012428571428571428, True, 10.0))
    assert calculate_dose_raw(0, 1_000_000_000, data, indexes) == pytest.approx((0.012428571428571428, 1.0))
    assert np.array_equal(calculate_peak_volts(data, indexes), np.array([[0.0, 3.0], [10.0, 3.0]]))


def test_analysis_uses_explicit_step_mode_and_handles_fewer_than_two_pulses() -> None:
    data, indexes = _waveform()

    stepped = analyze_snapshot(0, 1_000_000_000, data, indexes, is_step_exposure=True)
    continuous = analyze_snapshot(0, 1_000_000_000, data, indexes, is_step_exposure=False)
    empty = analyze_snapshot(0, 1_000_000_000, np.empty((0, 2)), np.empty((0, 2)))
    one_data, one_indexes = _waveform(pulse_count=1)
    one = analyze_snapshot(0, 1_000_000_000, one_data, one_indexes)

    assert stepped.inferred_step_exposure is False
    assert stepped.effective_duration_seconds == pytest.approx(10.0)
    assert stepped.total_dose_mj_cm2.hex() == "0x1.fd130463796acp-4"
    assert continuous.effective_duration_seconds == pytest.approx(1.0)
    assert empty.total_dose_mj_cm2 == 0.0
    assert empty.is_step_exposure is True
    assert one.pulse_doses_mj_cm2.shape == (1,)
    assert one.effective_duration_seconds == 0.0
    assert calculate_peak_volts(one_data, one_indexes) == (0.0, False, 0.0)


def test_analysis_keeps_dose_compensation_separate_from_exposure_runtime() -> None:
    data, indexes = _waveform()

    preinit = analyze_snapshot(0, 1_000_000_000, data, indexes, exposure_start_ns=None)
    crossing = analyze_snapshot(0, 1_000_000_000, data, indexes, exposure_start_ns=400_000_000)

    assert preinit.total_dose_mj_cm2 == pytest.approx(0.012428571428571428)
    assert preinit.effective_duration_seconds == pytest.approx(1.0)
    assert preinit.runtime_contribution_seconds == 0.0
    assert crossing.total_dose_mj_cm2 == pytest.approx(preinit.total_dose_mj_cm2)
    assert crossing.effective_duration_seconds == pytest.approx(1.0)
    assert crossing.runtime_contribution_seconds == pytest.approx(0.6)
    assert crossing.runtime_contribution_seconds.hex() == "0x1.3333333333333p-1"


def test_analysis_uses_persisted_step_and_runtime_context() -> None:
    data, indexes = _waveform()

    analysis = analyze_snapshot_from_metadata(
        0,
        1_000_000_000,
        data,
        indexes,
        {"is_step_exposure": True, "exposure_start_ns": 400_000_000},
    )

    assert analysis.is_step_exposure is True
    assert analysis.total_dose_mj_cm2 == pytest.approx(0.12428571428571428)
    assert analysis.runtime_contribution_seconds == pytest.approx(0.6)


def test_experiment_aggregation_keeps_preinit_dose_but_excludes_preinit_runtime() -> None:
    data, indexes = _waveform()
    snapshot = io.BytesIO()
    np.savez(snapshot, data=data, indexes=indexes)
    snapshot.seek(0)

    class _Reader:
        def get_snapshots(self, _experiment_id):
            return {
                uuid.uuid4(): (
                    snapshot,
                    io.StringIO(json.dumps({"start": 0, "end": 1_000_000_000, "exposure_start_ns": None})),
                )
            }

    dose, runtime = calculate_dose_of_experiment(uuid.uuid4(), _Reader())

    assert dose == pytest.approx(0.012428571428571428)
    assert runtime == 0.0


def test_analysis_rejects_malformed_indexes() -> None:
    data, _indexes = _waveform()

    with pytest.raises(ValueError, match="strictly increasing"):
        analyze_snapshot(0, 1_000_000_000, data, np.array([[40, 10.0], [0, 20.0]]))


class _Writer:
    def __init__(self) -> None:
        self.run_ids = []

    def set_exp_id(self, run_id) -> None:
        self.run_ids.append(run_id)


def _bare_oscilloscope_subsystem() -> OscilloscopeSubsystem:
    subsystem = object.__new__(OscilloscopeSubsystem)
    subsystem._OscilloscopeSubsystem__run_state_lock = threading.Lock()
    subsystem._OscilloscopeSubsystem__exp_id = None
    subsystem._OscilloscopeSubsystem__current_dose = 0.0
    subsystem._OscilloscopeSubsystem__current_time = 0.0
    subsystem._OscilloscopeSubsystem__target_dose = None
    subsystem._OscilloscopeSubsystem__target_time = None
    subsystem._OscilloscopeSubsystem__exposure_start_ns = None
    subsystem._OscilloscopeSubsystem__writer = _Writer()
    return subsystem


def test_duplicate_can_start_preserves_live_accumulators() -> None:
    subsystem = _bare_oscilloscope_subsystem()
    run_id = uuid.uuid4()
    state = RunState("exposure", ExposureSettings(target_dose=5.0), s_uuid=run_id)

    assert subsystem._can_start(state.get_settings(), state)[0] is True
    subsystem._OscilloscopeSubsystem__current_dose = 2.0
    subsystem._OscilloscopeSubsystem__current_time = 3.0
    subsystem._OscilloscopeSubsystem__exposure_start_ns = 123

    assert subsystem._can_start(state.get_settings(), state)[0] is True
    assert subsystem._OscilloscopeSubsystem__current_dose == 2.0
    assert subsystem._OscilloscopeSubsystem__current_time == 3.0
    assert subsystem._OscilloscopeSubsystem__exposure_start_ns == 123
    assert subsystem._OscilloscopeSubsystem__writer.run_ids == [run_id]


def test_running_state_sets_boundary_only_for_the_active_run() -> None:
    subsystem = _bare_oscilloscope_subsystem()
    active_run_id = uuid.uuid4()
    other_run_id = uuid.uuid4()
    subsystem._OscilloscopeSubsystem__exp_id = active_run_id
    active_state = RunState("exposure", ExposureSettings(target_dose=1.0), s_uuid=active_run_id)
    other_state = RunState("exposure", ExposureSettings(target_dose=1.0), s_uuid=other_run_id)

    other_payload = segment_bytes.encode(
        [ExperimentController.RUN_STATE_RUNNING.to_bytes(1, "big"), other_state.encode().encode("utf-8")]
    )
    active_payload = segment_bytes.encode(
        [ExperimentController.RUN_STATE_RUNNING.to_bytes(1, "big"), active_state.encode().encode("utf-8")]
    )
    subsystem._OscilloscopeSubsystem__on_exposure_state(other_payload)
    assert subsystem._OscilloscopeSubsystem__get_exposure_start_ns() is None

    subsystem._OscilloscopeSubsystem__on_exposure_state(active_payload)
    first_start = subsystem._OscilloscopeSubsystem__get_exposure_start_ns()
    subsystem._OscilloscopeSubsystem__on_exposure_state(active_payload)

    assert first_start is not None
    assert subsystem._OscilloscopeSubsystem__get_exposure_start_ns() == first_start


def test_scope_writer_flush_waits_for_pending_writes() -> None:
    writer = object.__new__(ScopeWriter)
    writer._ScopeWriter__write_condition = threading.Condition()
    writer._ScopeWriter__pending_writes = 0

    assert writer.flush(timeout=0.01)
    writer._ScopeWriter__pending_writes = 1
    assert not writer.flush(timeout=0.01)


def test_parallel_calc_reuses_workers_and_closes_cleanly() -> None:
    calculator = ParallelCalc(max_workers=2, thread_name_prefix="test-parallel-calc")
    try:
        assert calculator.max_workers == 2
        assert calculator.submit(threading.get_ident).result() > 0
        assert list(calculator.map(lambda value: value * 2, (1, 2, 3))) == [2, 4, 6]
    finally:
        calculator.close()

    with pytest.raises(RuntimeError, match="closed"):
        calculator.submit(threading.get_ident)

    with pytest.raises(ValueError, match="between 1 and 20"):
        ParallelCalc(max_workers=21)