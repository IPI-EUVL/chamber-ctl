import uuid

import pytest

from chamber_ctl.data.acquisition_runtime import LiveDoseAccumulator, PulseSequenceGap
from chamber_ctl.data.calibration import CalibrationProfile
from euv_acquisition.models import NativePulseAnalysis, PulseQuality, PulseReport


def _profile():
    return CalibrationProfile(
        profile_id=uuid.uuid4(),
        revision=1,
        name="Runtime test",
        created_at=1.0,
        algorithm_version="dose-v1",
        signal_polarity=1,
        load_resistance_ohms=50.0,
        photodiode_responsivity_a_per_w=0.14,
        illuminated_area_cm2=0.05,
    )


def _report(session_id, sequence, monotonic_ns, integral=0.14):
    return PulseReport(
        session_id=session_id,
        sequence=sequence,
        captured_at_unix_ns=monotonic_ns,
        captured_at_monotonic_ns=monotonic_ns,
        analysis=NativePulseAnalysis(0.0, integral, 0.0, 0.1, 0.1, PulseQuality.OK, "native-v1"),
    )


def test_live_accumulator_counts_all_measured_dose_but_only_transmitting_runtime() -> None:
    accumulator = LiveDoseAccumulator(_profile())
    session_id = uuid.uuid4()

    preinit = accumulator.ingest(_report(session_id, 0, 0))
    accumulator.set_running(True)
    accumulator.set_transmitting(True)
    running = accumulator.ingest(_report(session_id, 1, 10_000_000))
    next_running = accumulator.ingest(_report(session_id, 2, 20_000_000))

    assert preinit.pulse_dose_mj_cm2 > 0
    assert preinit.accumulated_dose_mj_cm2 == pytest.approx(preinit.pulse_dose_mj_cm2)
    assert running.transmitting_runtime_seconds == 0.0
    assert next_running.transmitting_runtime_seconds == pytest.approx(0.01)
    assert next_running.accumulated_dose_mj_cm2 == pytest.approx(preinit.pulse_dose_mj_cm2 * 3)


def test_live_accumulator_reports_a_500_ms_rolling_dose_rate() -> None:
    accumulator = LiveDoseAccumulator(_profile())
    session_id = uuid.uuid4()

    first = accumulator.ingest(_report(session_id, 0, 0))
    pulse_dose = first.pulse_dose_mj_cm2
    accumulator.ingest(_report(session_id, 1, 250_000_000))
    initial_rate = accumulator.ingest(_report(session_id, 2, 500_000_000))
    responsive_rate = accumulator.ingest(
        _report(session_id, 3, 750_000_000, integral=0.28)
    )

    assert first.dose_rate_mj_cm2_s is None
    assert initial_rate.dose_rate_mj_cm2_s == pytest.approx(4 * pulse_dose)
    assert responsive_rate.dose_rate_mj_cm2_s == pytest.approx(6 * pulse_dose)


def test_live_accumulator_rate_converges_over_a_96_hz_pulse_window() -> None:
    accumulator = LiveDoseAccumulator(_profile())
    session_id = uuid.uuid4()
    updates = [
        accumulator.ingest(
            _report(session_id, sequence, round(sequence * 1_000_000_000 / 96))
        )
        for sequence in range(49)
    ]

    assert updates[-1].dose_rate_mj_cm2_s == pytest.approx(
        updates[0].pulse_dose_mj_cm2 * 96,
        rel=1e-8,
    )


def test_live_accumulator_pauses_runtime_for_silence_but_preserves_pulse_dose() -> None:
    accumulator = LiveDoseAccumulator(_profile(), maximum_runtime_gap_seconds=0.25)
    session_id = uuid.uuid4()
    accumulator.set_running(True)
    accumulator.set_transmitting(True)
    accumulator.ingest(_report(session_id, 0, 0))
    update = accumulator.ingest(_report(session_id, 1, 300_000_000))

    assert update.transmitting_runtime_seconds == 0.0
    assert update.accumulated_dose_mj_cm2 > 0


def test_live_accumulator_deduplicates_replays_and_rejects_sequence_gaps() -> None:
    accumulator = LiveDoseAccumulator(_profile())
    session_id = uuid.uuid4()
    first = _report(session_id, 0, 0)
    accumulator.ingest(first)

    duplicate = accumulator.ingest(first)
    assert duplicate.accepted is False
    with pytest.raises(PulseSequenceGap, match="expected 1, received 2"):
        accumulator.ingest(_report(session_id, 2, 20_000_000))


def test_live_accumulator_records_unexpected_positive_dose_and_ignores_negative_noise() -> None:
    accumulator = LiveDoseAccumulator(_profile())
    session_id = uuid.uuid4()
    accumulator.set_running(True)
    accumulator.set_transmitting(False)

    blocked = accumulator.ingest(_report(session_id, 0, 0, integral=0.14))
    accumulator.set_transmitting(True)
    negative_noise = accumulator.ingest(_report(session_id, 1, 10_000_000, integral=-0.14))

    assert blocked.pulse_dose_mj_cm2 > 0
    assert blocked.accumulated_dose_mj_cm2 == pytest.approx(blocked.pulse_dose_mj_cm2)
    assert negative_noise.pulse_dose_mj_cm2 < 0
    assert negative_noise.accumulated_dose_mj_cm2 == pytest.approx(blocked.pulse_dose_mj_cm2)