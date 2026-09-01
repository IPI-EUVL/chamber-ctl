import json
import uuid

import numpy as np
import pytest

from chamber_ctl.data.calibration import CalibrationProfile
from chamber_ctl.data.dose_analysis import (
    CaptureTimelinePoint,
    DoseAnalysisResult,
    DoseAnalysisRevision,
    append_capture_timeline_point,
    analyze_hdf5_snapshot,
    analyze_experiment_entry,
    analyze_legacy_snapshot,
    legacy_default_profile,
    load_experiment_dose_series,
    load_experiment_peak_voltage_series,
    write_analysis_revision,
)
from ipi_ecs.db.db_library import Library


def _profile():
    return CalibrationProfile(
        profile_id=uuid.uuid4(),
        revision=1,
        name="Test profile",
        created_at=1.0,
        algorithm_version="dose-v1",
        signal_polarity=1,
        load_resistance_ohms=50.0,
        photodiode_responsivity_a_per_w=0.14,
        illuminated_area_cm2=0.05,
    )


def test_hdf5_analysis_recomputes_from_raw_samples(tmp_path) -> None:
    from euv_acquisition.analysis import analyze_pulse
    from euv_acquisition.models import CaptureConfig, CapturedPulse, PulseRecord, SnapshotCloseReason
    from euv_acquisition.snapshot import SnapshotStore

    config = CaptureConfig(sample_rate_hz=1_000_000.0, window_seconds=4e-6, pretrigger_seconds=1e-6)
    samples = np.array([0.1, 0.3, 0.3, 0.1], dtype=np.float32)
    session = uuid.uuid4()
    record = PulseRecord(session, 7, CapturedPulse(samples, 1, 1), analyze_pulse(samples, config))
    store = SnapshotStore(tmp_path)
    manifest = store.write([record], config, SnapshotCloseReason.CAPTURE_STOP, source_kind="simulated", source_id="test")
    profile = _profile()

    summary = analyze_hdf5_snapshot(store.path_for(manifest), profile)

    assert summary.source_format == "pitaya_hdf5"
    assert summary.first_sequence == summary.final_sequence == 7
    assert summary.total_dose_mj_cm2 == pytest.approx(profile.dose_for_integral(analyze_pulse(samples, config).integral_volt_seconds))


def test_hdf5_analysis_does_not_subtract_negative_noise(tmp_path) -> None:
    from euv_acquisition.analysis import analyze_pulse
    from euv_acquisition.models import CaptureConfig, CapturedPulse, PulseRecord, SnapshotCloseReason
    from euv_acquisition.snapshot import SnapshotStore

    config = CaptureConfig(sample_rate_hz=1_000_000.0, window_seconds=4e-6, pretrigger_seconds=1e-6)
    positive = np.array([0.0, 0.2, 0.2, 0.0], dtype=np.float32)
    negative = np.array([0.0, -0.3, -0.3, 0.0], dtype=np.float32)
    session = uuid.uuid4()
    records = [
        PulseRecord(session, index, CapturedPulse(samples, index, index), analyze_pulse(samples, config))
        for index, samples in enumerate((positive, negative))
    ]
    store = SnapshotStore(tmp_path)
    manifest = store.write(records, config, SnapshotCloseReason.CAPTURE_STOP, source_kind="simulated", source_id="test")
    profile = _profile()

    summary = analyze_hdf5_snapshot(store.path_for(manifest), profile)

    assert summary.total_dose_mj_cm2 == pytest.approx(
        profile.dose_for_integral(analyze_pulse(positive, config).integral_volt_seconds)
    )


def test_legacy_analysis_preserves_sequence_gap_compensation() -> None:
    profile = legacy_default_profile()
    waveform = np.column_stack(
        (
            np.arange(80, dtype=float) * 1e-9,
            np.tile(np.concatenate((np.zeros(25), np.full(15, 3.0))), 2),
        )
    )
    indexes = np.array([[0, 0.0], [40, 10.0]])

    summary = analyze_legacy_snapshot(
        uuid.uuid4(),
        0,
        1_000_000_000,
        waveform,
        indexes,
        {"is_step_exposure": False, "exposure_start_ns": 0},
        profile,
        source_sha256="f" * 64,
    )

    assert summary.total_dose_mj_cm2 == pytest.approx(summary.average_pulse_dose_mj_cm2 * 100.0)
    assert summary.runtime_seconds == pytest.approx(1.0)


def test_legacy_runtime_keeps_historical_missing_and_explicit_null_boundaries_distinct() -> None:
    profile = legacy_default_profile()
    waveform = np.column_stack(
        (
            np.arange(80, dtype=float) * 1e-9,
            np.tile(np.concatenate((np.zeros(25), np.full(15, 3.0))), 2),
        )
    )
    indexes = np.array([[0, 0.0], [40, 10.0]])
    common = (uuid.uuid4(), 0, 1_000_000_000, waveform, indexes)

    missing = analyze_legacy_snapshot(*common, {}, profile, source_sha256="e" * 64)
    preinit = analyze_legacy_snapshot(*common, {"exposure_start_ns": None}, profile, source_sha256="d" * 64)

    assert missing.runtime_seconds == pytest.approx(1.0)
    assert preinit.runtime_seconds == 0.0


def test_analysis_revisions_append_and_promote_without_overwriting_prior_resource(tmp_path) -> None:
    library = Library(tmp_path)
    entry = library.create_entry("Exposure", "Fixture")
    result = DoseAnalysisResult(uuid.uuid4(), _profile(), (), runtime_seconds=2.0)
    first = DoseAnalysisRevision(uuid.uuid4(), 10.0, result)
    second = DoseAnalysisRevision(uuid.uuid4(), 20.0, result)
    try:
        first_name = write_analysis_revision(entry, first, promote=True)
        second_name = write_analysis_revision(entry, second, promote=False)

        assert first_name in dict(entry.list_resources())
        assert second_name in dict(entry.list_resources())
        assert entry.get_tags()["active_dose_analysis"] == str(first.analysis_id)
        with entry.resource(first_name, "dose_analysis", "r") as resource:
            assert json.load(resource)["analysis_id"] == str(first.analysis_id)
    finally:
        library.close()


def test_experiment_entry_analysis_dispatches_hdf5_with_embedded_calibration(tmp_path) -> None:
    from euv_acquisition.analysis import analyze_pulse
    from euv_acquisition.models import CaptureConfig, CapturedPulse, PulseRecord, SnapshotCloseReason
    from euv_acquisition.snapshot import SnapshotStore

    profile = _profile()
    config = CaptureConfig(sample_rate_hz=1_000_000.0, window_seconds=4e-6, pretrigger_seconds=1e-6)
    samples = np.array([0.0, 0.2, 0.2, 0.0], dtype=np.float32)
    source_store = SnapshotStore(tmp_path / "source")
    source_manifest = source_store.write(
        [PulseRecord(uuid.uuid4(), 0, CapturedPulse(samples, 1, 1), analyze_pulse(samples, config))],
        config,
        SnapshotCloseReason.CAPTURE_STOP,
        source_kind="simulated",
        source_id="test",
    )
    records_path = tmp_path / "records"
    records_path.mkdir()
    library = Library(records_path)
    entry = library.create_entry("Exposure", "Fixture")
    try:
        with entry.resource("euv_calibration_profile.json", "euv_calibration_profile", "w") as resource:
            json.dump(profile.to_dict(), resource)
        with entry.resource(source_manifest.filename, "euv_snapshot", "wb") as resource:
            resource.write(source_store.path_for(source_manifest).read_bytes())
        result = analyze_experiment_entry(uuid.uuid4(), entry, runtime_seconds=0.01)

        assert result.calibration == profile
        assert result.total_dose_mj_cm2 > 0
        assert result.runtime_seconds == pytest.approx(0.01)
    finally:
        library.close()


def test_experiment_entry_analysis_ignores_non_authoritative_hdf5_sessions(tmp_path) -> None:
    from euv_acquisition.analysis import analyze_pulse
    from euv_acquisition.models import CaptureConfig, CapturedPulse, PulseRecord, SnapshotCloseReason
    from euv_acquisition.snapshot import SnapshotStore

    profile = _profile()
    config = CaptureConfig(sample_rate_hz=1_000_000.0, window_seconds=4e-6, pretrigger_seconds=1e-6)
    source_store = SnapshotStore(tmp_path / "source")
    primary_session = uuid.uuid4()
    observer_session = uuid.uuid4()
    primary_samples = np.array([0.0, 0.2, 0.2, 0.0], dtype=np.float32)
    observer_samples = np.array([0.0, 0.9, 0.9, 0.0], dtype=np.float32)
    primary = source_store.write(
        [PulseRecord(primary_session, 0, CapturedPulse(primary_samples, 1, 1), analyze_pulse(primary_samples, config))],
        config,
        SnapshotCloseReason.CAPTURE_STOP,
        source_kind="red_pitaya",
        source_id="primary",
    )
    observer = source_store.write(
        [PulseRecord(observer_session, 0, CapturedPulse(observer_samples, 2, 2), analyze_pulse(observer_samples, config))],
        config,
        SnapshotCloseReason.CAPTURE_STOP,
        source_kind="siglent",
        source_id="observer",
    )
    expected = analyze_hdf5_snapshot(source_store.path_for(primary), profile)
    records_path = tmp_path / "records"
    records_path.mkdir()
    library = Library(records_path)
    entry = library.create_entry("Exposure", "Mixed-source fixture")
    try:
        with entry.resource("euv_calibration_profile.json", "euv_calibration_profile", "w") as resource:
            json.dump(profile.to_dict(), resource)
        with entry.resource("euv_capture_session.json", "euv_capture_session", "w") as resource:
            json.dump(
                {
                    "session_id": str(primary_session),
                    "role": "authoritative",
                    "source_kind": "red_pitaya",
                    "source_id": "primary",
                },
                resource,
            )
        for manifest in (primary, observer):
            with entry.resource(manifest.filename, "euv_snapshot", "wb") as resource:
                resource.write(source_store.path_for(manifest).read_bytes())

        result = analyze_experiment_entry(uuid.uuid4(), entry, runtime_seconds=0.0)

        assert tuple(summary.snapshot_id for summary in result.snapshots) == (primary.snapshot_id,)
        assert result.total_dose_mj_cm2 == pytest.approx(expected.total_dose_mj_cm2)
    finally:
        library.close()


def test_experiment_entry_analysis_rejects_mismatched_authoritative_source_identity(tmp_path) -> None:
    from euv_acquisition.analysis import analyze_pulse
    from euv_acquisition.models import CaptureConfig, CapturedPulse, PulseRecord, SnapshotCloseReason
    from euv_acquisition.snapshot import SnapshotStore

    profile = _profile()
    config = CaptureConfig(sample_rate_hz=1_000_000.0, window_seconds=4e-6, pretrigger_seconds=1e-6)
    session_id = uuid.uuid4()
    samples = np.array([0.0, 0.2, 0.2, 0.0], dtype=np.float32)
    source_store = SnapshotStore(tmp_path / "source")
    manifest = source_store.write(
        [PulseRecord(session_id, 0, CapturedPulse(samples, 1, 1), analyze_pulse(samples, config))],
        config,
        SnapshotCloseReason.CAPTURE_STOP,
        source_kind="red_pitaya",
        source_id="actual",
    )
    records_path = tmp_path / "records"
    records_path.mkdir()
    library = Library(records_path)
    entry = library.create_entry("Exposure", "Mismatched-source fixture")
    try:
        with entry.resource("euv_calibration_profile.json", "euv_calibration_profile", "w") as resource:
            json.dump(profile.to_dict(), resource)
        with entry.resource("euv_capture_session.json", "euv_capture_session", "w") as resource:
            json.dump(
                {
                    "session_id": str(session_id),
                    "source_kind": "red_pitaya",
                    "source_id": "wrong",
                },
                resource,
            )
        with entry.resource(manifest.filename, "euv_snapshot", "wb") as resource:
            resource.write(source_store.path_for(manifest).read_bytes())

        with pytest.raises(ValueError, match="does not match HDF5 source identity"):
            analyze_experiment_entry(uuid.uuid4(), entry)
    finally:
        library.close()


def test_hdf5_dose_series_uses_measured_dose_and_exact_capture_runtime(tmp_path) -> None:
    from euv_acquisition.analysis import analyze_pulse
    from euv_acquisition.models import CaptureConfig, CapturedPulse, PulseRecord, SnapshotCloseReason
    from euv_acquisition.snapshot import SnapshotStore

    profile = _profile()
    config = CaptureConfig(sample_rate_hz=1_000_000.0, window_seconds=4e-6, pretrigger_seconds=1e-6)
    source_store = SnapshotStore(tmp_path / "source")
    session_id = uuid.uuid4()
    first_samples = np.array([0.0, 0.2, 0.2, 0.0], dtype=np.float32)
    second_samples = np.array([0.0, 0.3, 0.3, 0.0], dtype=np.float32)
    first = source_store.write(
        [PulseRecord(session_id, 0, CapturedPulse(first_samples, 10, 10), analyze_pulse(first_samples, config))],
        config,
        SnapshotCloseReason.PULSE_LIMIT,
        source_kind="simulated",
        source_id="test",
    )
    second = source_store.write(
        [PulseRecord(session_id, 1, CapturedPulse(second_samples, 20, 20), analyze_pulse(second_samples, config))],
        config,
        SnapshotCloseReason.CAPTURE_STOP,
        source_kind="simulated",
        source_id="test",
    )
    first_summary = analyze_hdf5_snapshot(source_store.path_for(first), profile)
    second_summary = analyze_hdf5_snapshot(source_store.path_for(second), profile)
    records_path = tmp_path / "records"
    records_path.mkdir()
    library = Library(records_path)
    entry = library.create_entry("Exposure", "Fixture")
    try:
        with entry.resource("euv_calibration_profile.json", "euv_calibration_profile", "w") as resource:
            json.dump(profile.to_dict(), resource)
        for manifest in (second, first):
            with entry.resource(manifest.filename, "euv_snapshot", "wb") as resource:
                resource.write(source_store.path_for(manifest).read_bytes())
        append_capture_timeline_point(entry, CaptureTimelinePoint(first.snapshot_id, 0, 0.0, 0.25))
        append_capture_timeline_point(entry, CaptureTimelinePoint(second.snapshot_id, 1, 0.0, 0.5))

        series = load_experiment_dose_series(uuid.uuid4(), entry)
        result = analyze_experiment_entry(uuid.uuid4(), entry)

        assert series.snapshot_ids == (first.snapshot_id, second.snapshot_id)
        assert series.cumulative_runtime_seconds.tolist() == [0.25, 0.5]
        assert series.cumulative_dose_mj_cm2.tolist() == [
            pytest.approx(first_summary.total_dose_mj_cm2),
            pytest.approx(first_summary.total_dose_mj_cm2 + second_summary.total_dose_mj_cm2),
        ]
        assert result.total_dose_mj_cm2 == pytest.approx(
            first_summary.total_dose_mj_cm2 + second_summary.total_dose_mj_cm2
        )
        assert result.live_total_dose_mj_cm2 == 0.0
        assert series.has_exact_runtime is True
        peaks = load_experiment_peak_voltage_series(entry)
        assert peaks.time_seconds.tolist() == [0.0, pytest.approx(1e-8)]
        assert peaks.peak_volts.tolist() == [pytest.approx(0.2), pytest.approx(0.3)]
    finally:
        library.close()