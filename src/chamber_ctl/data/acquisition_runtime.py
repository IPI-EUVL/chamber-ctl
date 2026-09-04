from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from uuid import UUID

from chamber_ctl.data.calibration import CalibrationProfile
from euv_acquisition.models import PulseReport


class PulseSequenceGap(ValueError):
    pass


@dataclass(frozen=True)
class LivePulseUpdate:
    accepted: bool
    pulse_dose_mj_cm2: float
    accumulated_dose_mj_cm2: float
    transmitting_runtime_seconds: float
    dose_rate_mj_cm2_s: float | None = None


class LiveDoseAccumulator:
    def __init__(
        self,
        calibration: CalibrationProfile,
        *,
        maximum_runtime_gap_seconds: float = 0.25,
        dose_rate_window_seconds: float = 0.5,
    ) -> None:
        if maximum_runtime_gap_seconds <= 0:
            raise ValueError("maximum_runtime_gap_seconds must be positive.")
        if not math.isfinite(dose_rate_window_seconds) or dose_rate_window_seconds <= 0:
            raise ValueError("dose_rate_window_seconds must be positive and finite.")
        self.calibration = calibration
        self.maximum_runtime_gap_ns = int(maximum_runtime_gap_seconds * 1e9)
        self.dose_rate_window_ns = int(dose_rate_window_seconds * 1e9)
        self.reset()

    def reset(self) -> None:
        self._session_id: UUID | None = None
        self._expected_sequence: int | None = None
        self._previous_monotonic_ns: int | None = None
        self._previous_was_running_and_transmitting = False
        self._running = False
        self._transmitting = False
        self.accumulated_dose_mj_cm2 = 0.0
        self.transmitting_runtime_seconds = 0.0
        self.dose_rate_mj_cm2_s: float | None = None
        self._dose_rate_samples: deque[tuple[int, float]] = deque()

    @property
    def running(self) -> bool:
        return self._running

    def set_running(self, running: bool) -> None:
        self._running = bool(running)

    def set_transmitting(self, transmitting: bool) -> None:
        self._transmitting = bool(transmitting)

    def ingest(self, report: PulseReport) -> LivePulseUpdate:
        if self._session_id is None:
            self._session_id = report.session_id
            self._expected_sequence = report.sequence
        elif report.session_id != self._session_id:
            raise PulseSequenceGap("Pulse report belongs to another capture session.")

        assert self._expected_sequence is not None
        if report.sequence < self._expected_sequence:
            return LivePulseUpdate(
                False,
                0.0,
                self.accumulated_dose_mj_cm2,
                self.transmitting_runtime_seconds,
                self.dose_rate_mj_cm2_s,
            )
        if report.sequence > self._expected_sequence:
            raise PulseSequenceGap(f"Pulse sequence gap: expected {self._expected_sequence}, received {report.sequence}.")

        pulse_dose = self.calibration.dose_for_integral(report.analysis.integral_volt_seconds)
        current_valid = self._running and self._transmitting
        if self._previous_monotonic_ns is not None and self._previous_was_running_and_transmitting and current_valid:
            elapsed = report.captured_at_monotonic_ns - self._previous_monotonic_ns
            if 0 <= elapsed <= self.maximum_runtime_gap_ns:
                self.transmitting_runtime_seconds += elapsed / 1e9
        self.accumulated_dose_mj_cm2 += max(0.0, pulse_dose)
        self._update_dose_rate(report.captured_at_monotonic_ns)
        self._previous_monotonic_ns = report.captured_at_monotonic_ns
        self._previous_was_running_and_transmitting = current_valid
        self._expected_sequence += 1
        return LivePulseUpdate(
            True,
            pulse_dose,
            self.accumulated_dose_mj_cm2,
            self.transmitting_runtime_seconds,
            self.dose_rate_mj_cm2_s,
        )

    def _update_dose_rate(self, captured_at_monotonic_ns: int) -> None:
        if self._dose_rate_samples and captured_at_monotonic_ns < self._dose_rate_samples[-1][0]:
            self._dose_rate_samples.clear()
        self._dose_rate_samples.append((captured_at_monotonic_ns, self.accumulated_dose_mj_cm2))
        cutoff_ns = captured_at_monotonic_ns - self.dose_rate_window_ns
        while len(self._dose_rate_samples) > 1 and self._dose_rate_samples[0][0] < cutoff_ns:
            self._dose_rate_samples.popleft()
        first_timestamp_ns, first_dose = self._dose_rate_samples[0]
        elapsed_ns = captured_at_monotonic_ns - first_timestamp_ns
        if elapsed_ns <= 0:
            self.dose_rate_mj_cm2_s = None
            return
        self.dose_rate_mj_cm2_s = (
            (self.accumulated_dose_mj_cm2 - first_dose) * 1e9 / elapsed_ns
        )