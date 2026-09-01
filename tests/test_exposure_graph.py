import json
from pathlib import Path
from types import SimpleNamespace
import uuid

import h5py
import numpy as np
import pytest

from chamber_ctl.data.calibration import CalibrationProfile
from chamber_ctl.data.dose_analysis import CaptureTimelinePoint, append_capture_timeline_point
from chamber_ctl.data.exposure_graph import (
    EXPOSURE_GRAPH_RESOURCE,
    ExposureGraphValidationError,
    _RawPulse,
    _apply_native_runtime,
    ensure_exposure_graph,
    read_exposure_graph,
)
from ipi_ecs.db.db_library import Library


def _profile() -> CalibrationProfile:
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


def test_native_runtime_anchor_rescaling_uses_the_unmodified_runtime_baseline() -> None:
    snapshot_id = uuid.uuid4()
    pulses = [
        _RawPulse(
            wall_unix_ns=1_000_000_000 + index * 100_000_000,
            monotonic_ns=10_000_000_000 + index * 100_000_000,
            dose_increment_mj_cm2=0.0,
            source_index=index,
            source_sequence=index,
            snapshot_id=snapshot_id,
        )
        for index in range(4)
    ]
    events = (
        SimpleNamespace(
            kind="lifecycle.phase",
            producer_unix_ns=0,
            sequence=0,
            next_sequence=None,
            payload={"phase": "RUNNING"},
        ),
        SimpleNamespace(
            kind="timing.euv_transmitting",
            producer_unix_ns=0,
            sequence=1,
            next_sequence=0,
            payload={"value": True},
        ),
    )
    timeline = (
        CaptureTimelinePoint(snapshot_id, 1, cumulative_dose_mj_cm2=0.0, cumulative_runtime_seconds=0.25),
        CaptureTimelinePoint(snapshot_id, 3, cumulative_dose_mj_cm2=0.0, cumulative_runtime_seconds=0.5),
    )

    runtime, quality = _apply_native_runtime(pulses, events, timeline)

    assert quality == "capture_timeline_anchored"
    assert np.all(np.diff(runtime) >= 0)
    assert runtime[1] == pytest.approx(0.25)
    assert runtime[3] == pytest.approx(0.5)


def test_native_graph_preserves_terminal_total_across_resolutions_and_rejects_tampering(tmp_path) -> None:
    from euv_acquisition.analysis import analyze_pulse
    from euv_acquisition.models import CaptureConfig, CapturedPulse, PulseRecord, SnapshotCloseReason
    from euv_acquisition.snapshot import SnapshotStore

    profile = _profile()
    config = CaptureConfig(sample_rate_hz=1_000_000.0, window_seconds=4e-6, pretrigger_seconds=1e-6)
    session_id = uuid.uuid4()
    source_store = SnapshotStore(tmp_path / "source")
    records = []
    for index in range(1_500):
        samples = np.array([0.0, 0.2 if index % 2 else 0.3, 0.2 if index % 2 else 0.3, 0.0], dtype=np.float32)
        records.append(
            PulseRecord(
                session_id,
                index,
                CapturedPulse(samples, 1_000_000_000 + index * 1_000_000, 10_000_000_000 + index * 1_000_000),
                analyze_pulse(samples, config),
            )
        )
    manifest = source_store.write(
        records,
        config,
        SnapshotCloseReason.CAPTURE_STOP,
        source_kind="simulated",
        source_id="test",
    )

    records_path = tmp_path / "records"
    records_path.mkdir()
    library = Library(records_path)
    entry = library.create_entry("Exposure", "Fixture")
    run_id = uuid.uuid4()
    try:
        with entry.resource("euv_calibration_profile.json", "euv_calibration_profile", "w") as resource:
            json.dump(profile.to_dict(), resource)
        with entry.resource(manifest.filename, "euv_snapshot", "wb") as resource:
            resource.write(source_store.path_for(manifest).read_bytes())
        append_capture_timeline_point(
            entry,
            CaptureTimelinePoint(manifest.snapshot_id, 1_499, cumulative_dose_mj_cm2=0.0, cumulative_runtime_seconds=2.0),
        )
        with entry.resource("end_metadata.json", "metadata", "w") as resource:
            json.dump({"outcome": "STOPPED"}, resource)

        created = ensure_exposure_graph(run_id, entry, records_path)
        graph = read_exposure_graph(entry, records_path, run_id)
        repeated = ensure_exposure_graph(run_id, entry, records_path)

        assert created.status == "generated"
        assert repeated.status == "existing"
        assert graph.raw_pulse_count == 1_500
        assert graph.full.point_count == 1_501
        assert graph.thumbnail.point_count == 1_000
        assert graph.full.cumulative_dose_mj_cm2[-1] == pytest.approx(graph.thumbnail.cumulative_dose_mj_cm2[-1])
        assert graph.full.runtime_seconds[-1] == pytest.approx(2.0)
        assert graph.thumbnail.runtime_seconds[-1] == pytest.approx(2.0)
        assert graph.full.represented_pulse_count.sum() == 1_500
        assert graph.thumbnail.represented_pulse_count.sum() == 1_500

        graph_path = Path(records_path) / entry.get_foldername() / EXPOSURE_GRAPH_RESOURCE
        with h5py.File(graph_path, "r+") as resource:
            resource["thumbnail/cumulative_dose_mj_cm2"][-1] += 1.0
        with pytest.raises(ExposureGraphValidationError):
            read_exposure_graph(entry, records_path, run_id)
    finally:
        library.close()


def test_legacy_graph_clamps_negative_compensated_snapshot_totals(tmp_path) -> None:
    run_id = uuid.uuid4()
    waveform = np.column_stack(
        (
            np.arange(80, dtype=float) * 1e-9,
            np.tile(np.concatenate((np.zeros(25), np.full(15, -3.0))), 2),
        )
    )
    indexes = np.array([[0, 0.0], [40, 0.01]])
    records_path = tmp_path / "records"
    records_path.mkdir()
    library = Library(records_path)
    entry = library.create_entry("Legacy exposure", "Negative correction fixture")
    snapshot_id = uuid.uuid4()
    try:
        with entry.resource(f"snap_{snapshot_id}.npz", "snapshot", "wb") as resource:
            np.savez(resource, data=waveform, indexes=indexes)
        with entry.resource(f"snap_{snapshot_id}.json", "snap_meta", "w") as resource:
            json.dump({"start": 0, "end": 1_000_000_000, "is_step_exposure": False}, resource)
        analysis_id = uuid.uuid4()
        entry.set_tag("active_dose_analysis", str(analysis_id))
        with entry.resource(f"dose_analysis_{analysis_id}.json", "dose_analysis", "w") as resource:
            json.dump({"total_dose_mj_cm2": -1.0, "runtime_seconds": 1.0}, resource)
        with entry.resource("end_metadata.json", "metadata", "w") as resource:
            json.dump({"outcome": "STOPPED"}, resource)

        result = ensure_exposure_graph(run_id, entry, records_path)
        graph = read_exposure_graph(entry, records_path, run_id)

        assert result.status == "generated"
        assert graph.full.cumulative_dose_mj_cm2[-1] == 0.0
        assert graph.thumbnail.cumulative_dose_mj_cm2[-1] == 0.0
        assert any("negative compensated dose total" in issue for issue in graph.issues)
        assert any("Graph dose differs from the active analysis" in issue for issue in graph.issues)
    finally:
        library.close()