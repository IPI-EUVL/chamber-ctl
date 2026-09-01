import shutil
import uuid

import numpy as np
import pytest

from chamber_ctl.data.acquisition_artifacts import AcquisitionArtifactImporter, ArtifactImportError
from ipi_ecs.db.db_library import Library


def _manifest(tmp_path):
    from euv_acquisition.analysis import analyze_pulse
    from euv_acquisition.models import CaptureConfig, CapturedPulse, PulseRecord, SnapshotCloseReason
    from euv_acquisition.snapshot import SnapshotStore

    config = CaptureConfig(sample_rate_hz=1_000_000.0, window_seconds=4e-6, pretrigger_seconds=1e-6)
    samples = np.array([0.0, 0.2, 0.2, 0.0], dtype=np.float32)
    record = PulseRecord(uuid.uuid4(), 0, CapturedPulse(samples, 1, 1), analyze_pulse(samples, config))
    store = SnapshotStore(tmp_path / "source")
    manifest = store.write([record], config, SnapshotCloseReason.CAPTURE_STOP, source_kind="simulated", source_id="test")
    return store, manifest


class _Client:
    def __init__(self, store, manifest, *, acknowledge_fails=False):
        self.store = store
        self.manifest = manifest
        self.acknowledge_fails = acknowledge_fails
        self.acknowledged = []

    def fetch_snapshot(self, snapshot_id, destination):
        if snapshot_id != self.manifest.snapshot_id:
            raise ValueError("unknown snapshot")
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.store.path_for(self.manifest), destination / self.manifest.filename)
        return self.manifest

    def command(self, command, payload):
        assert command == "acknowledge_snapshot"
        if self.acknowledge_fails:
            raise RuntimeError("network failed")
        self.acknowledged.append(payload["snapshot_id"])
        return {}


def test_importer_writes_verified_snapshot_before_acknowledging_source(tmp_path) -> None:
    store, manifest = _manifest(tmp_path)
    records_path = tmp_path / "records"
    records_path.mkdir()
    library = Library(records_path)
    entry = library.create_entry("Exposure", "Fixture")
    client = _Client(store, manifest)
    importer = AcquisitionArtifactImporter(client, tmp_path / "received")
    try:
        path = importer.import_snapshot(entry, manifest)

        assert path.is_file()
        assert dict(entry.list_resources())[manifest.filename] == "euv_snapshot"
        assert client.acknowledged == [str(manifest.snapshot_id)]
    finally:
        library.close()


def test_importer_reports_acknowledgement_failure_after_preserving_record_artifact(tmp_path) -> None:
    store, manifest = _manifest(tmp_path)
    records_path = tmp_path / "records"
    records_path.mkdir()
    library = Library(records_path)
    entry = library.create_entry("Exposure", "Fixture")
    importer = AcquisitionArtifactImporter(_Client(store, manifest, acknowledge_fails=True), tmp_path / "received")
    try:
        with pytest.raises(ArtifactImportError, match="acknowledgement failed"):
            importer.import_snapshot(entry, manifest)
        assert manifest.filename in dict(entry.list_resources())
    finally:
        library.close()


def test_importer_persists_timeline_before_acknowledging_source(tmp_path) -> None:
    store, manifest = _manifest(tmp_path)
    records_path = tmp_path / "records"
    records_path.mkdir()
    library = Library(records_path)
    entry = library.create_entry("Exposure", "Fixture")
    client = _Client(store, manifest)
    importer = AcquisitionArtifactImporter(client, tmp_path / "received")
    try:
        importer.import_snapshot(
            entry,
            manifest,
            before_ack=lambda persisted_entry: persisted_entry.set_tag("timeline_persisted", "yes"),
        )

        assert entry.get_tags()["timeline_persisted"] == "yes"
        assert client.acknowledged == [str(manifest.snapshot_id)]
    finally:
        library.close()


def test_importer_exposes_verified_persisted_file_before_acknowledgement(tmp_path) -> None:
    store, manifest = _manifest(tmp_path)
    records_path = tmp_path / "records"
    records_path.mkdir()
    library = Library(records_path)
    entry = library.create_entry("Exposure", "Fixture")
    client = _Client(store, manifest)
    observed = []
    importer = AcquisitionArtifactImporter(client, tmp_path / "received")
    try:
        importer.import_snapshot(
            entry,
            manifest,
            after_persist=lambda persisted, path: observed.append(
                (path.is_file(), manifest.filename in dict(persisted.list_resources()), list(client.acknowledged))
            ),
        )

        assert observed == [(True, True, [])]
        assert client.acknowledged == [str(manifest.snapshot_id)]
    finally:
        library.close()


def test_importer_source_validation_runs_before_record_write_or_acknowledgement(tmp_path) -> None:
    store, manifest = _manifest(tmp_path)
    records_path = tmp_path / "records"
    records_path.mkdir()
    library = Library(records_path)
    entry = library.create_entry("Exposure", "Fixture")
    client = _Client(store, manifest)
    importer = AcquisitionArtifactImporter(client, tmp_path / "received")
    try:
        with pytest.raises(ArtifactImportError, match="source-specific validation"):
            importer.import_snapshot(
                entry,
                manifest,
                validate=lambda _manifest, _path: (_ for _ in ()).throw(
                    ValueError("wrong observer source")
                ),
            )

        assert manifest.filename not in dict(entry.list_resources())
        assert client.acknowledged == []
    finally:
        library.close()