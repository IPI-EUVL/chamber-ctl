from __future__ import annotations

import threading
import uuid
from pathlib import Path

import pytest

import chamber_ctl.data.registry_repair as registry_repair
from chamber_ctl.data.registry_repair import (
    infer_resource_type,
    log_applied_repair,
    read_registry,
    repair_exposure_registries,
)
from ipi_ecs.db.db_library import Entry, Library
from ipi_ecs.logging.client import LogClient
from ipi_ecs.logging.protocol import PROTO_V1, TYPE_LOG, decode_log_record, decode_message


def _create_run(library: Library, name: str, experiment_type: str = "exposure") -> Entry:
    entry = library.create_entry(name, "Fixture")
    entry.set_tag("experiment", experiment_type)
    entry.set_tag("run", uuid.uuid4().hex)
    with entry.resource("run.json", "run_state", "w") as resource:
        resource.write("{}")
    with entry.resource("metadata.json", "metadata", "w") as resource:
        resource.write("{}")
    return entry


def _folder(root: Path, entry: Entry) -> Path:
    return root / entry.get_foldername()


def test_repair_recognizes_persisted_graph_resource_types() -> None:
    assert infer_resource_type("euv_exposure_dose_graph.h5") == "euv_exposure_dose_graph"
    assert infer_resource_type("euv_capture_cadence.h5") == "euv_capture_cadence"


def test_repair_dry_run_only_scans_exposure_records(tmp_path: Path) -> None:
    library = Library(str(tmp_path))
    healthy = _create_run(library, "Healthy")
    zero_snapshots = _create_run(library, "Zero snapshots")
    missing_registry = _create_run(library, "Missing registry")
    unrelated = _create_run(library, "Unrelated", "experiment")

    healthy_folder = _folder(tmp_path, healthy)
    with healthy.resource(f"snap_{uuid.uuid4()}.npz", "snapshot", "wb") as resource:
        resource.write(b"snapshot")

    zero_folder = _folder(tmp_path, zero_snapshots)
    snapshot_id = uuid.uuid4()
    (zero_folder / f"snap_{snapshot_id}.npz").write_bytes(b"snapshot")
    (zero_folder / f"snap_{snapshot_id}.json").write_text("{}", encoding="utf-8")

    missing_path = _folder(tmp_path, missing_registry) / "registry.dat"
    missing_path.unlink()
    library.close()

    healthy_before = (healthy_folder / "registry.dat").read_bytes()
    zero_before = (zero_folder / "registry.dat").read_bytes()
    results = {result.entry_uuid: result for result in repair_exposure_registries(tmp_path)}

    assert results[healthy.get_uuid()].action == "registry_matches_disk"
    assert results[zero_snapshots.get_uuid()].action == "would_reconcile_snapshots"
    assert results[missing_registry.get_uuid()].action == "would_rebuild_missing"
    assert unrelated.get_uuid() not in results
    assert (healthy_folder / "registry.dat").read_bytes() == healthy_before
    assert (zero_folder / "registry.dat").read_bytes() == zero_before
    assert not missing_path.exists()


def test_repair_only_updates_zero_snapshot_and_missing_registries(tmp_path: Path) -> None:
    library = Library(str(tmp_path))
    healthy = _create_run(library, "Healthy")
    zero_snapshots = _create_run(library, "Zero snapshots")
    no_snapshot_files = _create_run(library, "No snapshot files")
    missing_registry = _create_run(library, "Missing registry")
    missing_without_snapshots = _create_run(library, "Missing without snapshots")

    healthy_folder = _folder(tmp_path, healthy)
    healthy_snapshot_id = uuid.uuid4()
    with healthy.resource(f"snap_{healthy_snapshot_id}.npz", "snapshot", "wb") as resource:
        resource.write(b"snapshot")
    healthy_before = (healthy_folder / "registry.dat").read_bytes()

    zero_folder = _folder(tmp_path, zero_snapshots)
    recovered_snapshot_id = uuid.uuid4()
    (zero_folder / f"snap_{recovered_snapshot_id}.npz").write_bytes(b"snapshot")
    (zero_folder / f"snap_{recovered_snapshot_id}.json").write_text("{}", encoding="utf-8")

    no_files_path = _folder(tmp_path, no_snapshot_files) / "registry.dat"
    no_files_before = no_files_path.read_bytes()

    missing_folder = _folder(tmp_path, missing_registry)
    missing_snapshot_id = uuid.uuid4()
    (missing_folder / f"snap_{missing_snapshot_id}.npz").write_bytes(b"snapshot")
    (missing_folder / f"snap_{missing_snapshot_id}.json").write_text("{}", encoding="utf-8")
    (missing_folder / "laser_config.bin").write_bytes(b"laser")
    (missing_folder / "operator_notes.txt").write_text("notes", encoding="utf-8")
    (missing_folder / "registry.dat").unlink()

    missing_without_snapshots_folder = _folder(tmp_path, missing_without_snapshots)
    (missing_without_snapshots_folder / "registry.dat").unlink()
    library.close()

    results = {result.entry_uuid: result for result in repair_exposure_registries(tmp_path, apply=True)}

    assert results[healthy.get_uuid()].action == "registry_matches_disk"
    assert results[zero_snapshots.get_uuid()].action == "reconciled_snapshots"
    assert results[no_snapshot_files.get_uuid()].action == "registry_matches_disk"
    assert results[missing_registry.get_uuid()].action == "rebuilt_missing"
    assert results[missing_without_snapshots.get_uuid()].action == "rebuilt_missing"
    assert (healthy_folder / "registry.dat").read_bytes() == healthy_before
    assert no_files_path.read_bytes() == no_files_before

    repaired = read_registry(zero_folder / "registry.dat")
    assert repaired.resources[f"snap_{recovered_snapshot_id}.npz"] == "snapshot"
    assert repaired.resources[f"snap_{recovered_snapshot_id}.json"] == "snap_meta"
    assert (zero_folder / "registry.dat.pre-repair").is_file()

    rebuilt = read_registry(missing_folder / "registry.dat")
    assert rebuilt.resources["run.json"] == "run_state"
    assert rebuilt.resources["metadata.json"] == "metadata"
    assert rebuilt.resources[f"snap_{missing_snapshot_id}.npz"] == "snapshot"
    assert rebuilt.resources[f"snap_{missing_snapshot_id}.json"] == "snap_meta"
    assert rebuilt.resources["laser_config.bin"] == "Laser Config"
    assert rebuilt.resources["operator_notes.txt"] == "recovered"

    rebuilt_without_snapshots = read_registry(missing_without_snapshots_folder / "registry.dat")
    assert rebuilt_without_snapshots.resources == {
        "metadata.json": "metadata",
        "run.json": "run_state",
    }


def test_repair_reconciles_partially_registered_snapshots(tmp_path: Path) -> None:
    library = Library(str(tmp_path))
    entry = _create_run(library, "Partial registry")
    folder = _folder(tmp_path, entry)

    registered_snapshot_id = uuid.uuid4()
    with entry.resource(f"snap_{registered_snapshot_id}.npz", "snapshot", "wb") as resource:
        resource.write(b"registered")
    with entry.resource(f"snap_{registered_snapshot_id}.json", "snap_meta", "w") as resource:
        resource.write("{}")

    missing_snapshot_id = uuid.uuid4()
    missing_waveform = f"snap_{missing_snapshot_id}.npz"
    missing_metadata = f"snap_{missing_snapshot_id}.json"
    (folder / missing_waveform).write_bytes(b"not registered")
    (folder / missing_metadata).write_text("{}", encoding="utf-8")
    library.close()

    result = next(repair_exposure_registries(tmp_path))

    assert result.action == "would_reconcile_snapshots"
    assert result.registered_snapshots == 1
    assert result.disk_snapshots == 2
    assert dict(result.disk_resources)[missing_waveform] == "snapshot"
    assert dict(result.registered_resources)[f"snap_{registered_snapshot_id}.npz"] == "snapshot"
    assert dict(result.registry_writes) == {
        missing_metadata: "snap_meta",
        missing_waveform: "snapshot",
    }

    applied = next(repair_exposure_registries(tmp_path, apply=True))
    assert applied.action == "reconciled_snapshots"
    repaired = read_registry(folder / "registry.dat")
    assert repaired.resources[missing_waveform] == "snapshot"
    assert repaired.resources[missing_metadata] == "snap_meta"


def test_repair_recognizes_unregistered_hdf5_euv_snapshots(tmp_path: Path) -> None:
    library = Library(str(tmp_path))
    entry = _create_run(library, "HDF5 snapshot")
    folder = _folder(tmp_path, entry)
    snapshot_id = uuid.uuid4()
    snapshot_name = f"snap_{snapshot_id}.h5"
    (folder / snapshot_name).write_bytes(b"hdf5 fixture")
    library.close()

    result = next(repair_exposure_registries(tmp_path, apply=True))

    assert result.action == "reconciled_snapshots"
    assert result.disk_snapshots == 1
    assert read_registry(folder / "registry.dat").resources[snapshot_name] == "euv_snapshot"


def test_applied_repair_logs_one_structured_before_and_after_record(tmp_path: Path) -> None:
    library = Library(str(tmp_path))
    entry = _create_run(library, "Audited repair")
    folder = _folder(tmp_path, entry)
    snapshot_id = uuid.uuid4()
    waveform_name = f"snap_{snapshot_id}.npz"
    metadata_name = f"snap_{snapshot_id}.json"
    (folder / waveform_name).write_bytes(b"snapshot")
    (folder / metadata_name).write_text("{}", encoding="utf-8")
    library.close()

    dry_run = next(repair_exposure_registries(tmp_path))
    applied = next(repair_exposure_registries(tmp_path, apply=True))

    class RecordingSocket:
        def __init__(self):
            self.payloads = []

        def put(self, payload):
            self.payloads.append(payload)

    socket = RecordingSocket()
    logger = LogClient(socket)
    assert log_applied_repair(logger, dry_run) is False
    assert log_applied_repair(logger, applied) is True
    assert len(socket.payloads) == 1

    message_type, version, payload = decode_message(socket.payloads[0])
    record = decode_log_record(payload)
    data = record["data"]
    assert message_type == TYPE_LOG
    assert version == PROTO_V1
    assert "Repaired exposure registry" in record["msg"]
    assert str(applied.run_uuid) in record["msg"]
    assert record["level"] == "INFO"
    assert record["l_type"] == "DATOP"
    assert data["event"] == "exposure_registry_repair"
    assert data["before"]["resources"] == {
        "metadata.json": "metadata",
        "run.json": "run_state",
    }
    assert data["after"]["resources"] == {
        "metadata.json": "metadata",
        "run.json": "run_state",
        metadata_name: "snap_meta",
        waveform_name: "snapshot",
    }
    assert data["registry_writes"] == {
        metadata_name: "snap_meta",
        waveform_name: "snapshot",
    }


def test_missing_registry_audit_marks_before_state_as_absent(tmp_path: Path) -> None:
    library = Library(str(tmp_path))
    entry = _create_run(library, "Missing audited registry")
    registry_path = _folder(tmp_path, entry) / "registry.dat"
    registry_path.unlink()
    library.close()

    applied = next(repair_exposure_registries(tmp_path, apply=True))

    class RecordingSocket:
        def __init__(self):
            self.payloads = []

        def put(self, payload):
            self.payloads.append(payload)

    socket = RecordingSocket()
    assert log_applied_repair(LogClient(socket), applied) is True
    _, _, payload = decode_message(socket.payloads[0])
    data = decode_log_record(payload)["data"]

    assert data["action"] == "rebuilt_missing"
    assert data["before"] == {
        "exists": False,
        "state": "missing",
        "size_bytes": None,
        "resource_count": None,
        "snapshot_waveform_count": None,
        "resources": None,
    }
    assert data["after"]["exists"] is True
    assert data["after"]["resources"] == {
        "metadata.json": "metadata",
        "run.json": "run_state",
    }


def test_repair_rebuilds_and_audits_zero_byte_registry(tmp_path: Path) -> None:
    library = Library(str(tmp_path))
    entry = _create_run(library, "Empty registry")
    folder = _folder(tmp_path, entry)
    snapshot_id = uuid.uuid4()
    waveform_name = f"snap_{snapshot_id}.npz"
    metadata_name = f"snap_{snapshot_id}.json"
    (folder / waveform_name).write_bytes(b"snapshot")
    (folder / metadata_name).write_text("{}", encoding="utf-8")
    registry_path = folder / "registry.dat"
    registry_path.write_bytes(b"")
    library.close()

    dry_run = next(repair_exposure_registries(tmp_path, max_workers=1))

    assert dry_run.action == "would_rebuild_empty"
    assert dry_run.registry_state_before == "empty"
    assert dry_run.registry_size_before == 0
    assert dry_run.registered_resources == ()
    assert dict(dry_run.registry_writes) == {
        metadata_name: "snap_meta",
        "metadata.json": "metadata",
        "run.json": "run_state",
        waveform_name: "snapshot",
    }
    assert registry_path.read_bytes() == b""

    applied = next(repair_exposure_registries(tmp_path, apply=True, max_workers=1))

    assert applied.action == "rebuilt_empty"
    assert applied.changed is True
    assert (folder / "registry.dat.pre-repair").read_bytes() == b""
    rebuilt = read_registry(registry_path)
    assert rebuilt.resources == dict(applied.registry_after)

    class RecordingSocket:
        def __init__(self):
            self.payloads = []

        def put(self, payload):
            self.payloads.append(payload)

    socket = RecordingSocket()
    assert log_applied_repair(LogClient(socket), applied) is True
    _, _, payload = decode_message(socket.payloads[0])
    record = decode_log_record(payload)

    assert "rebuilt an empty registry" in record["msg"]
    assert record["data"]["before"] == {
        "exists": True,
        "state": "empty",
        "size_bytes": 0,
        "resource_count": None,
        "snapshot_waveform_count": None,
        "resources": None,
    }


def test_repair_reports_registry_snapshot_declarations_missing_on_disk(tmp_path: Path) -> None:
    library = Library(str(tmp_path))
    entry = _create_run(library, "Missing snapshot file")
    snapshot_id = uuid.uuid4()
    waveform_name = f"snap_{snapshot_id}.npz"
    with entry.resource(waveform_name, "snapshot", "wb") as resource:
        resource.write(b"snapshot")
    (_folder(tmp_path, entry) / waveform_name).unlink()
    library.close()

    result = next(repair_exposure_registries(tmp_path, apply=True))

    assert result.action == "registry_references_missing_snapshot_files"
    assert result.changed is False
    assert result.disk_snapshots == 0
    assert result.registered_snapshots == 1
    assert result.registry_writes == ()
    assert result.registry_only_snapshot_resources == ((waveform_name, "snapshot"),)
    assert read_registry(_folder(tmp_path, entry) / "registry.dat").resources[waveform_name] == "snapshot"


def test_repair_inspects_in_parallel_and_applies_on_the_calling_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library = Library(str(tmp_path))
    entries = [_create_run(library, f"Run {index}") for index in range(4)]
    for entry in entries:
        snapshot_id = uuid.uuid4()
        folder = _folder(tmp_path, entry)
        (folder / f"snap_{snapshot_id}.npz").write_bytes(b"snapshot")
        (folder / f"snap_{snapshot_id}.json").write_text("{}", encoding="utf-8")
    library.close()

    caller_thread = threading.get_ident()
    active_inspections = 0
    peak_inspections = 0
    inspection_threads = set()
    inspection_lock = threading.Lock()
    parallel_release = threading.Event()
    real_discover_resources = registry_repair._discover_resources

    def track_discover_resources(folder):
        nonlocal active_inspections, peak_inspections
        with inspection_lock:
            inspection_threads.add(threading.get_ident())
            active_inspections += 1
            peak_inspections = max(peak_inspections, active_inspections)
            if active_inspections == 2:
                parallel_release.set()
        try:
            if not parallel_release.wait(timeout=2.0):
                raise TimeoutError("Registry inspections did not overlap.")
            return real_discover_resources(folder)
        finally:
            with inspection_lock:
                active_inspections -= 1

    write_threads = []
    real_atomic_write = registry_repair._atomic_write_registry

    def track_atomic_write(*args, **kwargs):
        write_threads.append(threading.get_ident())
        return real_atomic_write(*args, **kwargs)

    monkeypatch.setattr(registry_repair, "_discover_resources", track_discover_resources)
    monkeypatch.setattr(registry_repair, "_atomic_write_registry", track_atomic_write)

    results = list(repair_exposure_registries(tmp_path, apply=True, max_workers=2))

    assert peak_inspections == 2
    assert caller_thread not in inspection_threads
    assert write_threads == [caller_thread] * len(entries)
    assert {result.action for result in results} == {"reconciled_snapshots"}


def test_repair_reuses_one_library_per_worker_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library = Library(str(tmp_path))
    entries = [_create_run(library, f"Run {index}") for index in range(5)]
    library.close()

    real_library = registry_repair.Library
    instances = []
    instances_lock = threading.Lock()

    class TrackingLibrary:
        def __init__(self, *args, **kwargs):
            self.inner = real_library(*args, **kwargs)
            self.created_thread = threading.get_ident()
            self.closed_thread = None
            self.read_count = 0
            with instances_lock:
                instances.append(self)

        def query(self, *args, **kwargs):
            return self.inner.query(*args, **kwargs)

        def read_entry(self, *args, **kwargs):
            self.read_count += 1
            return self.inner.read_entry(*args, **kwargs)

        def close(self):
            self.closed_thread = threading.get_ident()
            self.inner.close()

    monkeypatch.setattr(registry_repair, "Library", TrackingLibrary)

    results = list(repair_exposure_registries(tmp_path, max_workers=2))

    coordinator_libraries = [instance for instance in instances if instance.created_thread == threading.get_ident()]
    worker_libraries = [instance for instance in instances if instance.created_thread != threading.get_ident()]
    assert len(results) == len(entries)
    assert len(coordinator_libraries) == 1
    assert len(worker_libraries) == 2
    assert sum(instance.read_count for instance in worker_libraries) == len(entries)
    assert any(instance.read_count > 1 for instance in worker_libraries)
    assert all(instance.closed_thread == instance.created_thread for instance in instances)


@pytest.mark.parametrize("workers", (0, 21, True))
def test_repair_rejects_invalid_worker_counts(tmp_path: Path, workers) -> None:
    with pytest.raises(ValueError, match="between 1 and 20"):
        list(repair_exposure_registries(tmp_path, max_workers=workers))