import json
from pathlib import Path
import uuid

import h5py
import numpy as np
import pytest

from chamber_ctl.data.capture_cadence_graph import (
    CAPTURE_CADENCE_GRAPH_RESOURCE,
    CaptureCadenceGraphValidationError,
    ensure_capture_cadence_graph,
    read_capture_cadence_graph,
)
from ipi_ecs.db.db_library import Library
from ipi_ecs.subsystems.run_events import RunEventStream, append_run_event


def _write_provenance(entry, session_id: uuid.UUID, chopper_frequency_hz: float = 200.0) -> None:
    with entry.resource("euv_capture_session.json", "euv_capture_session", "w") as resource:
        json.dump(
            {
                "session_id": str(session_id),
                "calibration_profile_id": str(uuid.uuid4()),
                "calibration_revision": 1,
                "calibration_hash": "fixture",
                "chopper_frequency_hz": chopper_frequency_hz,
            },
            resource,
        )


def _write_snapshots(source_path: Path, entry, session_id: uuid.UUID, groups: list[list[tuple[int, int]]]) -> None:
    from euv_acquisition.analysis import analyze_pulse
    from euv_acquisition.models import CaptureConfig, CapturedPulse, PulseRecord, SnapshotCloseReason
    from euv_acquisition.snapshot import SnapshotStore

    config = CaptureConfig(sample_rate_hz=1_000_000.0, window_seconds=4e-6, pretrigger_seconds=1e-6)
    store = SnapshotStore(source_path)
    for group in groups:
        records = []
        for sequence, offset_ns in group:
            samples = np.asarray([0.0, 0.2, 0.2, 0.0], dtype=np.float32)
            records.append(
                PulseRecord(
                    session_id,
                    sequence,
                    CapturedPulse(samples, 1_000_000_000 + offset_ns, 10_000_000_000 + offset_ns),
                    analyze_pulse(samples, config),
                )
            )
        manifest = store.write(
            records,
            config,
            SnapshotCloseReason.CAPTURE_STOP,
            source_kind="simulated",
            source_id="cadence-fixture",
        )
        with entry.resource(manifest.filename, "euv_snapshot", "wb") as resource:
            resource.write(store.path_for(manifest).read_bytes())


def _entry(tmp_path: Path, groups: list[list[tuple[int, int]]]):
    records_path = tmp_path / "records"
    records_path.mkdir()
    library = Library(records_path)
    entry = library.create_entry("Exposure", "Cadence fixture")
    run_id = uuid.uuid4()
    session_id = uuid.uuid4()
    _write_provenance(entry, session_id)
    _write_snapshots(tmp_path / "source", entry, session_id, groups)
    with entry.resource("end_metadata.json", "metadata", "w") as resource:
        json.dump({"outcome": "STOPPED"}, resource)
    return records_path, library, entry, run_id, session_id


def test_persisted_cadence_infers_gap_across_snapshots_and_rejects_tampering(tmp_path) -> None:
    records_path, library, entry, run_id, _session_id = _entry(
        tmp_path,
        [[(0, 0), (1, 10_000_000)], [(2, 40_000_000), (3, 50_000_000)]],
    )
    try:
        created = ensure_capture_cadence_graph(run_id, entry, records_path)
        graph = read_capture_cadence_graph(entry, records_path, run_id)
        repeated = ensure_capture_cadence_graph(run_id, entry, records_path)

        assert created.status == "generated"
        assert repeated.status == "existing"
        assert graph.expected_rate_hz == 100.0
        assert graph.raw_capture_count == 4
        assert graph.inferred_lost_count == 2
        assert graph.gap_estimated_lost_count.tolist() == [2]
        assert graph.gap_crosses_snapshot_boundary.tolist() == [True]
        assert graph.capture_rate_hz.shape == (3, 4)
        assert any("Trigger-state timeline was unavailable" in issue for issue in graph.issues)

        graph_path = records_path / entry.get_foldername() / CAPTURE_CADENCE_GRAPH_RESOURCE
        with h5py.File(graph_path, "r+") as resource:
            resource.attrs["inferred_lost_count"] = 99
        with pytest.raises(CaptureCadenceGraphValidationError, match="loss total"):
            read_capture_cadence_graph(entry, records_path, run_id)
    finally:
        library.close()


def test_trigger_disabled_interval_starts_a_new_cadence_segment(tmp_path) -> None:
    records_path, library, entry, run_id, session_id = _entry(
        tmp_path,
        [[(0, 0), (1, 10_000_000)], [(2, 1_000_000_000), (3, 1_010_000_000)]],
    )
    producer_id = uuid.uuid4()
    stream = RunEventStream(run_id, uuid.uuid4(), "acquisition.timing", producer_id)
    events = (
        stream.event(
            "stream.start",
            {},
            producer_unix_ns=999_999_000,
            capture_session_id=session_id,
        ),
        stream.event(
            "timing.triggers_enabled",
            {"value": True},
            producer_unix_ns=999_999_100,
            capture_session_id=session_id,
            next_sequence=0,
        ),
        stream.event(
            "timing.triggers_enabled",
            {"value": False},
            producer_unix_ns=1_020_000_000,
            capture_session_id=session_id,
            next_sequence=2,
        ),
        stream.event(
            "timing.triggers_enabled",
            {"value": True},
            producer_unix_ns=1_900_000_000,
            capture_session_id=session_id,
            next_sequence=2,
        ),
        stream.event(
            "stream.end",
            {},
            producer_unix_ns=2_020_000_000,
            capture_session_id=session_id,
            next_sequence=4,
        ),
    )
    try:
        for event in events:
            append_run_event(entry, event)

        graph = ensure_capture_cadence_graph(run_id, entry, records_path).graph

        assert graph is not None
        assert graph.inferred_lost_count == 0
        assert graph.gap_estimated_lost_count.size == 0
        assert graph.segment_id.tolist() == [0, 0, 1, 1]
    finally:
        library.close()