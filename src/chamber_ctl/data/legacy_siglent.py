from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np


LEGACY_ANALYSIS_VERSION = "legacy-siglent-v1-sequence-gap-compensation"
EXPOSURE_START_UNSPECIFIED = object()


@dataclass(frozen=True)
class LegacySiglentAnalysis:
    average_pulse_dose_mj_cm2: float
    pulse_times_seconds: np.ndarray
    pulse_indexes: np.ndarray
    pulse_doses_mj_cm2: np.ndarray
    pulse_peaks_volts: np.ndarray
    pulse_span_seconds: float
    wall_duration_seconds: float
    is_step_exposure: bool
    inferred_step_exposure: bool
    effective_duration_seconds: float
    runtime_contribution_seconds: float
    total_dose_mj_cm2: float
    delivered_dose_rate_mj_cm2_s: float


def legacy_siglent_dose_for_integral(integral_volt_seconds: float) -> float:
    return ((integral_volt_seconds / 50.0) / 0.14) * 1000.0 / 0.05


def analyze_legacy_siglent_values(
    start,
    end,
    pulse_times_seconds,
    pulse_indexes,
    pulse_doses_mj_cm2,
    pulse_peaks_volts,
    *,
    is_step_exposure: bool | None = None,
    exposure_start_ns=EXPOSURE_START_UNSPECIFIED,
) -> LegacySiglentAnalysis:
    pulse_times = np.asarray(pulse_times_seconds, dtype=float)
    sample_indexes = np.asarray(pulse_indexes, dtype=int)
    pulse_dose_array = np.asarray(pulse_doses_mj_cm2, dtype=float)
    pulse_peak_array = np.asarray(pulse_peaks_volts, dtype=float)
    lengths = {len(pulse_times), len(sample_indexes), len(pulse_dose_array), len(pulse_peak_array)}
    if len(lengths) != 1:
        raise ValueError("Legacy Siglent pulse arrays must have equal lengths.")
    if any(array.ndim != 1 for array in (pulse_times, sample_indexes, pulse_dose_array, pulse_peak_array)):
        raise ValueError("Legacy Siglent pulse values must be one-dimensional.")
    if any(not np.isfinite(array).all() for array in (pulse_times, pulse_dose_array, pulse_peak_array)):
        raise ValueError("Legacy Siglent pulse values must be finite.")
    if len(pulse_times) > 1 and np.any(np.diff(pulse_times) < 0):
        raise ValueError("Legacy Siglent pulse times must not decrease.")
    if is_step_exposure is not None and not isinstance(is_step_exposure, bool):
        raise ValueError("Snapshot is_step_exposure must be boolean when provided.")

    try:
        start_ns = float(start)
        end_ns = float(end)
        wall_duration = (end_ns - start_ns) / 1e9
    except (TypeError, ValueError) as exc:
        raise ValueError("Snapshot start and end timestamps must be numeric nanoseconds.") from exc
    if not math.isfinite(wall_duration) or wall_duration < 0:
        raise ValueError("Snapshot end timestamp must not precede its start timestamp.")

    average_pulse_dose = float(np.average(pulse_dose_array)) if len(pulse_dose_array) else 0.0
    pulse_span = float(pulse_times[-1] - pulse_times[0]) if len(pulse_times) > 1 else 0.0
    inferred_step = len(pulse_dose_array) < 2 or float(np.average(pulse_dose_array[-50:])) < 0.1 * average_pulse_dose
    step_exposure = inferred_step if is_step_exposure is None else is_step_exposure
    effective_duration = pulse_span if step_exposure else wall_duration
    total_dose = average_pulse_dose * effective_duration * 100.0
    uncorrected_dose = average_pulse_dose * pulse_span * 100.0
    delivered_rate = uncorrected_dose / pulse_span if pulse_span > 0 else 0.0

    runtime_contribution = effective_duration
    if exposure_start_ns is None:
        runtime_contribution = 0.0
    elif exposure_start_ns is not EXPOSURE_START_UNSPECIFIED:
        try:
            exposure_start = float(exposure_start_ns)
        except (TypeError, ValueError) as exc:
            raise ValueError("Snapshot exposure start timestamp must be numeric when provided.") from exc
        if not math.isfinite(exposure_start):
            raise ValueError("Snapshot exposure start timestamp must be finite when provided.")
        runtime_contribution = max(0.0, end_ns - max(start_ns, exposure_start)) / 1e9

    return LegacySiglentAnalysis(
        average_pulse_dose_mj_cm2=average_pulse_dose,
        pulse_times_seconds=pulse_times.copy(),
        pulse_indexes=sample_indexes.copy(),
        pulse_doses_mj_cm2=pulse_dose_array,
        pulse_peaks_volts=pulse_peak_array,
        pulse_span_seconds=pulse_span,
        wall_duration_seconds=wall_duration,
        is_step_exposure=step_exposure,
        inferred_step_exposure=inferred_step,
        effective_duration_seconds=effective_duration,
        runtime_contribution_seconds=runtime_contribution,
        total_dose_mj_cm2=total_dose,
        delivered_dose_rate_mj_cm2_s=delivered_rate,
    )


def analyze_legacy_siglent_snapshot(
    start,
    end,
    data,
    indexes,
    *,
    dose_for_integral: Callable[[float], float] = legacy_siglent_dose_for_integral,
    is_step_exposure: bool | None = None,
    exposure_start_ns=EXPOSURE_START_UNSPECIFIED,
) -> LegacySiglentAnalysis:
    waveform = np.asarray(data, dtype=float)
    pulse_indexes = np.asarray(indexes)
    if waveform.ndim != 2 or waveform.shape[1] < 2:
        raise ValueError("Snapshot data must contain time and voltage columns.")
    if pulse_indexes.ndim != 2 or pulse_indexes.shape[1] < 2:
        raise ValueError("Snapshot indexes must contain sample index and pulse time columns.")
    if not np.isfinite(waveform[:, :2]).all():
        raise ValueError("Snapshot data contains non-finite values.")

    try:
        sample_index_values = np.asarray(pulse_indexes[:, 0], dtype=float)
        pulse_times = np.asarray(pulse_indexes[:, 1], dtype=float)
    except (TypeError, ValueError) as exc:
        raise ValueError("Snapshot indexes must be numeric.") from exc
    if not np.isfinite(sample_index_values).all() or not np.isfinite(pulse_times).all():
        raise ValueError("Snapshot indexes contain non-finite values.")
    if not np.equal(sample_index_values, np.floor(sample_index_values)).all():
        raise ValueError("Snapshot sample indexes must be integers.")
    sample_indexes = sample_index_values.astype(int)
    if np.any(sample_indexes < 0) or np.any(sample_indexes >= len(waveform)):
        raise ValueError("Snapshot sample indexes are outside the waveform.")
    if len(sample_indexes) > 1 and (np.any(np.diff(sample_indexes) <= 0) or np.any(np.diff(pulse_times) < 0)):
        raise ValueError("Snapshot indexes must be strictly increasing with non-decreasing pulse times.")
    if is_step_exposure is not None and not isinstance(is_step_exposure, bool):
        raise ValueError("Snapshot is_step_exposure must be boolean when provided.")

    pulse_doses = []
    pulse_peaks = []
    for position, sample_index in enumerate(sample_indexes):
        stop = sample_indexes[position + 1] if position + 1 < len(sample_indexes) else len(waveform)
        pulse = waveform[sample_index:stop, :2]
        baseline = float(np.average(pulse[: min(25, len(pulse)), 1]))
        corrected_volts = pulse[:, 1] - baseline
        integral_volt_seconds = float(np.trapezoid(corrected_volts, pulse[:, 0]))
        pulse_doses.append(dose_for_integral(integral_volt_seconds))
        pulse_peaks.append(float(np.max(pulse[:, 1])))

    return analyze_legacy_siglent_values(
        start,
        end,
        pulse_times,
        sample_indexes,
        pulse_doses,
        pulse_peaks,
        is_step_exposure=is_step_exposure,
        exposure_start_ns=exposure_start_ns,
    )