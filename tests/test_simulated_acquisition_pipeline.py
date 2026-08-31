import time
import uuid
from collections import deque

import numpy as np

from chamber_ctl.data.acquisition_artifacts import AcquisitionArtifactImporter
from chamber_ctl.data.acquisition_preview import AcquisitionPreview
from chamber_ctl.data.acquisition_runtime import LiveDoseAccumulator
from chamber_ctl.data.calibration import CalibrationProfile
from chamber_ctl.data.dose_analysis import DoseAnalysisResult, DoseAnalysisRevision, analyze_hdf5_snapshot, write_analysis_revision
from chamber_ctl.subsystems.euv_acquisition_controller import EuvAcquisitionSubsystem, write_capture_provenance
from chamber_ctl.subsystems.exposure_controller import ExposureSettings
from ipi_ecs.db.db_library import Library
from ipi_ecs.subsystems.experiment_controller import ExperimentReader, RunState

from euv_acquisition.models import CaptureConfig, CapturedPulse
from euv_acquisition.service import AcquisitionClient, AcquisitionServer, ServiceConfig
from euv_acquisition.session import CaptureEngine, RotationConfig, SpoolRepository
from euv_acquisition.snapshot import SnapshotStore


class _QueuePulseSource:
    def __init__(self) -> None:
        self.capture_config = CaptureConfig(sample_rate_hz=1_000_000.0, window_seconds=4e-6, pretrigger_seconds=1e-6)
        self._timestamps = deque((1, 2))
        self._open = False

    def open(self) -> None:
        self._open = True

    def capture(self):
        if not self._open or not self._timestamps:
            return None
        timestamp = self._timestamps.popleft()
        return CapturedPulse(np.array([0.0, 0.2, 0.2, 0.0], dtype=np.float32), timestamp, timestamp)

    def close(self) -> None:
        self._open = False


def _calibration() -> CalibrationProfile:
    return CalibrationProfile(
        profile_id=uuid.uuid4(),
        revision=1,
        name="Pipeline calibration",
        created_at=1.0,
        algorithm_version="dose-v1",
        signal_polarity=1,
        load_resistance_ohms=50.0,
        photodiode_responsivity_a_per_w=0.14,
        illuminated_area_cm2=0.05,
    )


def test_simulated_pipeline_persists_reconciled_hdf5_dose_before_releasing_spool(tmp_path) -> None:
    spool_path = tmp_path / "spool"
    source = _QueuePulseSource()
    store = SnapshotStore(spool_path)
    engine = CaptureEngine(
        source,
        store,
        SpoolRepository(spool_path),
        source_kind="simulated",
        source_id="pipeline",
        rotation=RotationConfig(pulse_limit=2, trigger_idle_seconds=100.0),
    )
    server = AcquisitionServer(engine, ServiceConfig(control_port=0, artifact_port=0, capture_poll_seconds=0.001))
    server.start()
    client = AcquisitionClient(server.control_address, server.artifact_address)
    records_path = tmp_path / "records"
    records_path.mkdir()
    library = Library(records_path)
    entry = library.create_entry("Exposure", "Pipeline fixture")
    calibration = _calibration()
    accumulator = LiveDoseAccumulator(calibration)
    accumulator.set_running(True)
    accumulator.set_transmitting(True)
    try:
        client.connect()
        session_id = uuid.uuid4()
        client.command("start_capture", {"session_id": str(session_id)})
        report_one = client.get_report(timeout=1.0)
        report_two = client.get_report(timeout=1.0)
        accumulator.ingest(__import__("euv_acquisition.models", fromlist=["PulseReport"]).PulseReport.from_dict(report_one))
        accumulator.ingest(__import__("euv_acquisition.models", fromlist=["PulseReport"]).PulseReport.from_dict(report_two))
        manifest = client.get_snapshot(timeout=1.0)

        imported = AcquisitionArtifactImporter(client, tmp_path / "imported").import_snapshot(entry, manifest)
        client.command("stop_capture", {"reason": "pipeline test complete"})
        summary = analyze_hdf5_snapshot(imported, calibration)
        result = DoseAnalysisResult(uuid.uuid4(), calibration, (summary,), accumulator.transmitting_runtime_seconds, accumulator.accumulated_dose_mj_cm2)
        revision = DoseAnalysisRevision(uuid.uuid4(), time.time(), result)
        write_analysis_revision(entry, revision, promote=True)
        client.command("release_snapshots")

        assert entry.get_tags()["dose"] == summary.total_dose_mj_cm2
        assert entry.get_tags()["active_dose_analysis"] == str(revision.analysis_id)
        assert manifest.filename in dict(entry.list_resources())
        assert server.engine.spool.load() is None
    finally:
        client.close()
        server.close()
        library.close()


def test_orphaned_session_recovery_imports_reconciles_and_releases_spool(tmp_path, monkeypatch) -> None:
    spool_path = tmp_path / "spool"
    capture_source = _QueuePulseSource()
    snapshot_store = SnapshotStore(spool_path)
    spool = SpoolRepository(spool_path)
    original_engine = CaptureEngine(
        capture_source,
        snapshot_store,
        spool,
        source_kind="simulated",
        source_id="orphan-fixture",
        rotation=RotationConfig(pulse_limit=1, trigger_idle_seconds=100.0),
    )
    session_id = uuid.uuid4()
    original_engine.start(session_id)
    original_engine.capture_once()

    restarted_engine = CaptureEngine(
        _QueuePulseSource(),
        snapshot_store,
        spool,
        source_kind="simulated",
        source_id="orphan-fixture",
        rotation=RotationConfig(pulse_limit=1, trigger_idle_seconds=100.0),
    )
    server = AcquisitionServer(restarted_engine, ServiceConfig(control_port=0, artifact_port=0, capture_poll_seconds=0.001))
    server.start()
    records_path = tmp_path / "records"
    records_path.mkdir()
    library = Library(records_path)
    run_uuid = uuid.uuid4()
    entry = library.create_entry("Exposure", "Orphan recovery fixture")
    entry.set_tag("experiment", "exposure")
    entry.set_tag("run", run_uuid.hex)
    state = RunState("exposure", ExposureSettings(target_dose=1.0), s_uuid=run_uuid)
    with entry.resource("run.json", "run_state", "w") as resource:
        resource.write(state.encode())
    with entry.resource("metadata.json", "metadata", "w") as resource:
        resource.write('{"created_at":1.0,"version":1}')
    write_capture_provenance(entry, session_id, _calibration(), 192.0)
    reader = ExperimentReader(str(records_path), "exposure")
    subsystem = object.__new__(EuvAcquisitionSubsystem)
    subsystem._reader = reader
    subsystem._capture_client = None
    subsystem._artifact_importer = None
    subsystem._temporary_directory = None
    subsystem._next_capture_connect_monotonic = 0.0
    try:
        monkeypatch.setenv("EUV_ACQUISITION_HOST", server.control_address[0])
        monkeypatch.setenv("EUV_ACQUISITION_CONTROL_PORT", str(server.control_address[1]))
        monkeypatch.setenv("EUV_ACQUISITION_ARTIFACT_PORT", str(server.artifact_address[1]))

        message = subsystem._recover_orphaned_capture_session()
        persistent_client = subsystem._capture_client
        subsystem._check_capture_service_ready()

        assert "Recovered orphaned capture session" in message
        assert spool.load() is None
        assert subsystem._capture_client is persistent_client
        recovered_entry = reader.get_run(run_uuid).get_record()
        resources = dict(recovered_entry.list_resources())
        assert any(filename.endswith(".h5") for filename in resources)
        assert "active_dose_analysis" in recovered_entry.get_tags()
    finally:
        reader.close()
        server.close()
        library.close()


def test_one_shot_diagnostic_publishes_preview_and_cleans_both_hosts(tmp_path, monkeypatch) -> None:
    from chamber_ctl.subsystems.euv_acquisition_controller import _DiagnosticCapture

    class _Publisher:
        def __init__(self) -> None:
            self.values = []

        @property
        def value(self):
            return self.values[-1] if self.values else None

        @value.setter
        def value(self, payload):
            self.values.append(payload)

    class _Handle:
        def __init__(self) -> None:
            self.returned = []
            self.failed = []

        def feedback(self, _value) -> None:
            pass

        def ret(self, value) -> None:
            self.returned.append(value)

        def fail(self, value) -> None:
            self.failed.append(value)

    spool_path = tmp_path / "diagnostic-spool"
    source = _QueuePulseSource()
    source._timestamps = deque((1,))
    store = SnapshotStore(spool_path)
    spool = SpoolRepository(spool_path)
    engine = CaptureEngine(
        source,
        store,
        spool,
        source_kind="simulated",
        source_id="diagnostic-pipeline",
        rotation=RotationConfig(pulse_limit=10, trigger_idle_seconds=100.0),
    )
    server = AcquisitionServer(engine, ServiceConfig(control_port=0, artifact_port=0, capture_poll_seconds=0.001))
    server.start()
    publisher = _Publisher()
    handle = _Handle()
    subsystem = object.__new__(EuvAcquisitionSubsystem)
    subsystem._run_lock = __import__("threading").RLock()
    subsystem._run = None
    subsystem._diagnostic = None
    subsystem._diagnostic_start_pending = True
    subsystem._diagnostic_error = None
    subsystem._capture_client = None
    subsystem._artifact_importer = None
    subsystem._temporary_directory = None
    subsystem._next_capture_connect_monotonic = 0.0
    subsystem._board_status = {}
    subsystem._preview_publisher = publisher
    subsystem._log = lambda *_args, **_kwargs: None
    try:
        monkeypatch.setenv("EUV_ACQUISITION_HOST", server.control_address[0])
        monkeypatch.setenv("EUV_ACQUISITION_CONTROL_PORT", str(server.control_address[1]))
        monkeypatch.setenv("EUV_ACQUISITION_ARTIFACT_PORT", str(server.artifact_address[1]))

        assert subsystem._start_diagnostic("one_shot", handle) is None
        assert isinstance(subsystem._diagnostic, _DiagnosticCapture)
        deadline = time.monotonic() + 2.0
        while subsystem._diagnostic is not None and time.monotonic() < deadline:
            subsystem._consume_capture_events()
            subsystem._advance_diagnostic()
            time.sleep(0.01)

        assert subsystem._diagnostic is None
        assert handle.failed == []
        assert handle.returned
        assert spool.load() is None
        assert len(publisher.values) == 1
        preview = AcquisitionPreview.decode(publisher.values[0])
        assert preview.context == "diagnostic"
        assert preview.source_id == "diagnostic-pipeline"
        assert not list(subsystem._temporary_directory.glob("*.h5"))
    finally:
        subsystem._close_capture_client()
        server.close()