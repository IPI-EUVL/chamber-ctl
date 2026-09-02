import json
import shutil
import uuid
from dataclasses import replace

import numpy as np
import pytest

from chamber_ctl.data.acquisition_artifacts import ArtifactImportError
from chamber_ctl.data.calibration import (
    CalibrationProfile,
    CalibrationRepository,
    SourceCalibrationBinding,
    SourceKey,
    source_calibration_run_tags,
)
from chamber_ctl.data.observer_artifacts import (
    OBSERVER_CAPTURE_RESOURCE_TYPE,
    OBSERVER_CONTEXT_RESOURCE_TYPE,
    ObserverArtifactRecorder,
    observer_capture_filename,
    observer_context_filename,
)
from chamber_ctl.data.observer_analysis import (
    CAPTURED_ALGORITHM,
    LEGACY_COMPENSATED_ALGORITHM,
    OBSERVER_ANALYSIS_RESOURCE_TYPE,
    OBSERVER_GRAPH_RESOURCE_TYPE,
    load_observer_dose_products,
    observer_analysis_filename,
    observer_graph_filename,
)
from chamber_ctl.subsystems.exposure_controller import ExposureSettings
from chamber_ctl.subsystems.siglent_observer import ObserverCaptureRun, ObserverTimingObservation
from euv_acquisition.models import (
    CaptureConfig,
    CapturedPulse,
    NativePulseAnalysis,
    PulseQuality,
    PulseRecord,
    SnapshotCloseReason,
    SourceBatchEnvelope,
)
from euv_acquisition.session import CaptureSessionManifest, CaptureSessionState, StoredSnapshot
from euv_acquisition.snapshot import SnapshotStore
from euv_acquisition.sources.siglent import SIGLENT_BATCH_KIND, SIGLENT_NATIVE_ANALYSIS_VERSION
from ipi_ecs.db.db_library import Library
from ipi_ecs.subsystems.experiment_controller import RunState


SOURCE = SourceKey("siglent", "scope-1")


class _Client:
    def __init__(self, store, manifest) -> None:
        self.store = store
        self.manifest = manifest
        self.acknowledged = []

    def fetch_snapshot(self, snapshot_id, destination):
        assert snapshot_id == self.manifest.snapshot_id
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.store.path_for(self.manifest), destination / self.manifest.filename)
        return self.manifest

    def command(self, command, payload):
        assert command == "acknowledge_snapshot"
        self.acknowledged.append(payload["snapshot_id"])
        return {}


def _profile() -> CalibrationProfile:
    return CalibrationProfile(
        profile_id=uuid.uuid4(),
        revision=1,
        name="Siglent fixture",
        created_at=1.0,
        algorithm_version="test",
        signal_polarity=-1,
        load_resistance_ohms=50.0,
        photodiode_responsivity_a_per_w=0.14,
        illuminated_area_cm2=0.05,
    )


def _run_record(data_path, run_id, settings, *, source_calibrations=()) -> None:
    library = Library(data_path)
    try:
        entry = library.create_entry("Exposure", "Observer fixture")
        entry.set_tag("experiment", "exposure")
        entry.set_tag("run", run_id.hex)
        for key, value in source_calibration_run_tags(source_calibrations).items():
            entry.set_tag(key, value)
        with entry.resource("run.json", "run_state", "w") as resource:
            resource.write(RunState("exposure", settings, run_id).encode())
        with entry.resource("metadata.json", "metadata", "w") as resource:
            resource.write('{"created_at":1.0,"version":1}')
    finally:
        library.close()


def _snapshot(tmp_path, session_id, *, analysis_version=SIGLENT_NATIVE_ANALYSIS_VERSION):
    config = CaptureConfig(sample_rate_hz=1_000_000.0, window_seconds=4e-6, pretrigger_seconds=1e-6)
    samples = np.asarray([0.0, -0.2, -0.1, 0.0], dtype=np.float32)
    analysis = NativePulseAnalysis(
        baseline_volts=0.0,
        integral_volt_seconds=-3e-7,
        minimum_volts=-0.2,
        maximum_volts=0.0,
        peak_absolute_volts=0.2,
        quality=PulseQuality(0),
        algorithm_version=analysis_version,
    )
    record = PulseRecord(session_id, 0, CapturedPulse(samples, 20, 20), analysis)
    envelope = SourceBatchEnvelope(uuid.uuid4(), SIGLENT_BATCH_KIND, 10, 30)
    store = SnapshotStore(tmp_path / "source")
    manifest = store.write(
        [record],
        config,
        SnapshotCloseReason.SOURCE_BATCH,
        source_kind=SOURCE.source_kind,
        source_id=SOURCE.source_id,
        source_batch=envelope,
    )
    return store, manifest


def _fixture(tmp_path, *, analysis_version=SIGLENT_NATIVE_ANALYSIS_VERSION, timing_observations=()):
    data_path = tmp_path / "records"
    data_path.mkdir()
    profile = _profile()
    repository = CalibrationRepository(data_path)
    try:
        repository.create(profile)
    finally:
        repository.close()
    binding = SourceCalibrationBinding(SOURCE.source_kind, SOURCE.source_id, profile.profile_id, profile.revision)
    run_id = uuid.uuid4()
    session_id = uuid.uuid4()
    _run_record(data_path, run_id, ExposureSettings(), source_calibrations=(binding,))
    store, manifest = _snapshot(tmp_path, session_id, analysis_version=analysis_version)
    session = CaptureSessionManifest(
        server_boot_id=uuid.uuid4(),
        session_id=session_id,
        state=CaptureSessionState.STOPPED,
        source_kind=SOURCE.source_kind,
        source_id=SOURCE.source_id,
        started_at_unix_ns=5,
        snapshots=(StoredSnapshot(manifest),),
        final_sequence=0,
        stop_reason="fixture complete",
    )
    run = ObserverCaptureRun(
        run_id,
        session_id,
        SOURCE,
        binding,
        5,
        15,
        40,
        tuple(timing_observations),
    )
    return data_path, run, store, manifest, session


def test_observer_recorder_persists_descriptor_and_context_before_acknowledgement(tmp_path) -> None:
    data_path, run, store, manifest, session = _fixture(tmp_path)
    client = _Client(store, manifest)
    recorder = ObserverArtifactRecorder(data_path, SOURCE, temporary_directory=tmp_path / "received")

    assert recorder.resolve_calibration(run.run_id) == run.calibration

    recorder.prepare_run(run, client)
    recorder.finalize_run(run, session, client)

    library = Library(data_path)
    try:
        entry = library.query({"tags": {"run": run.run_id.hex}}, limit=1)[0]
        resources = dict(entry.list_resources())
        assert resources[manifest.filename] == "euv_snapshot"
        assert resources[observer_capture_filename(run.session_id)] == OBSERVER_CAPTURE_RESOURCE_TYPE
        assert resources[observer_context_filename(run.session_id)] == OBSERVER_CONTEXT_RESOURCE_TYPE
        for algorithm in (CAPTURED_ALGORITHM, LEGACY_COMPENSATED_ALGORITHM):
            assert resources[observer_analysis_filename(run.session_id, algorithm)] == OBSERVER_ANALYSIS_RESOURCE_TYPE
            assert resources[observer_graph_filename(run.session_id, algorithm)] == OBSERVER_GRAPH_RESOURCE_TYPE
        with entry.resource(observer_capture_filename(run.session_id), OBSERVER_CAPTURE_RESOURCE_TYPE, "r") as resource:
            descriptor = json.load(resource)
        with entry.resource(observer_context_filename(run.session_id), OBSERVER_CONTEXT_RESOURCE_TYPE, "r") as resource:
            context = json.load(resource)
        assert descriptor["status"] == "complete"
        assert descriptor["snapshot_ids"] == [str(manifest.snapshot_id)]
        assert context["snapshots"][0]["exposure_start_ns"] == {"state": "value", "value": 15}
        assert context["snapshots"][0]["is_step_exposure"] == {"state": "unknown"}
        assert context["snapshots"][0]["laser_off_eligibility"] == {
            "state": "unknown",
            "evidence_count": 0,
            "clock_bases": [],
        }
        assert client.acknowledged == [str(manifest.snapshot_id)]
        products = {product.analysis.algorithm: product for product in load_observer_dose_products(entry, data_path, run.run_id)}
        captured = products[CAPTURED_ALGORITHM].analysis
        legacy = products[LEGACY_COMPENSATED_ALGORITHM].analysis
        assert captured.calibration.profile_id == run.calibration.profile_id
        assert captured.calibration.revision == run.calibration.revision
        assert captured.total_dose_mj_cm2 == pytest.approx(captured.calibration.dose_for_integral(-3e-7))
        assert captured.pulse_count == 1
        assert captured.status == "complete"
        assert legacy.calibration.signal_polarity == 1
        assert legacy.status == "incomplete"
        assert legacy.completeness.unknown_eligibility_snapshot_count == 1
        assert legacy.completeness.unknown_step_mode_snapshot_count == 1
        assert "active_dose_analysis" not in entry.get_tags()
    finally:
        library.close()


def test_observer_recorder_recovers_after_acknowledgement_before_product_finalization(
    tmp_path,
    monkeypatch,
) -> None:
    import chamber_ctl.data.observer_artifacts as observer_artifacts

    data_path, run, store, manifest, session = _fixture(tmp_path)
    client = _Client(store, manifest)
    recorder = ObserverArtifactRecorder(data_path, SOURCE, temporary_directory=tmp_path / "received")
    recorder.prepare_run(run, client)
    write_products = observer_artifacts.write_observer_dose_products
    monkeypatch.setattr(
        observer_artifacts,
        "write_observer_dose_products",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("interrupted after acknowledgement")),
    )

    with pytest.raises(RuntimeError, match="interrupted after acknowledgement"):
        recorder.finalize_run(run, session, client)

    assert client.acknowledged == [str(manifest.snapshot_id)]
    acknowledged_session = replace(
        session,
        snapshots=(replace(session.snapshots[0], acknowledged=True),),
    )
    monkeypatch.setattr(observer_artifacts, "write_observer_dose_products", write_products)
    recovered = ObserverArtifactRecorder(data_path, SOURCE, temporary_directory=tmp_path / "received")
    recovered.prepare_run(run, client)
    recovered.finalize_run(run, acknowledged_session, client)

    library = Library(data_path)
    try:
        entry = library.query({"tags": {"run": run.run_id.hex}}, limit=1)[0]
        with entry.resource(observer_capture_filename(run.session_id), OBSERVER_CAPTURE_RESOURCE_TYPE, "r") as resource:
            descriptor = json.load(resource)
        with entry.resource(observer_context_filename(run.session_id), OBSERVER_CONTEXT_RESOURCE_TYPE, "r") as resource:
            context = json.load(resource)
        products = load_observer_dose_products(entry, data_path, run.run_id)
    finally:
        library.close()

    assert descriptor["status"] == "complete"
    assert context["snapshots"][0]["snapshot_id"] == str(manifest.snapshot_id)
    assert len(products) == 2
    assert client.acknowledged == [str(manifest.snapshot_id)]


def test_observer_recorder_rejects_wrong_native_analysis_before_write_or_ack(tmp_path) -> None:
    data_path, run, store, manifest, session = _fixture(tmp_path, analysis_version="wrong-version")
    client = _Client(store, manifest)
    recorder = ObserverArtifactRecorder(data_path, SOURCE, temporary_directory=tmp_path / "received")
    recorder.prepare_run(run, client)

    with pytest.raises(ArtifactImportError, match="wrong native analysis version"):
        recorder.finalize_run(run, session, client)

    library = Library(data_path)
    try:
        entry = library.query({"tags": {"run": run.run_id.hex}}, limit=1)[0]
        assert manifest.filename not in dict(entry.list_resources())
        assert client.acknowledged == []
    finally:
        library.close()


def test_observer_recorder_marks_batch_ineligible_from_laser_timing_evidence(tmp_path) -> None:
    timing = (
        ObserverTimingObservation(15, True, "laser_status"),
        ObserverTimingObservation(25, False, "laser_status"),
    )
    data_path, run, store, manifest, session = _fixture(tmp_path, timing_observations=timing)
    client = _Client(store, manifest)
    recorder = ObserverArtifactRecorder(data_path, SOURCE, temporary_directory=tmp_path / "received")
    recorder.prepare_run(run, client)
    recorder.finalize_run(run, session, client)

    library = Library(data_path)
    try:
        entry = library.query({"tags": {"run": run.run_id.hex}}, limit=1)[0]
        with entry.resource(observer_context_filename(run.session_id), OBSERVER_CONTEXT_RESOURCE_TYPE, "r") as resource:
            context = json.load(resource)
        assert context["snapshots"][0]["laser_off_eligibility"] == {
            "state": "ineligible",
            "evidence_count": 2,
            "clock_bases": ["laser_status"],
        }
    finally:
        library.close()


def test_observer_recorder_accepts_timing_metadata_added_after_prepare(tmp_path) -> None:
    data_path, run, store, manifest, session = _fixture(tmp_path)
    client = _Client(store, manifest)
    recorder = ObserverArtifactRecorder(data_path, SOURCE, temporary_directory=tmp_path / "received")
    recorder.prepare_run(run, client)
    finalized_run = replace(
        run,
        timing_observations=(ObserverTimingObservation(20, True, "laser_status"),),
    )

    recorder.finalize_run(finalized_run, session, client)

    library = Library(data_path)
    try:
        entry = library.query({"tags": {"run": run.run_id.hex}}, limit=1)[0]
        with entry.resource(observer_capture_filename(run.session_id), OBSERVER_CAPTURE_RESOURCE_TYPE, "r") as resource:
            descriptor = json.load(resource)
        assert descriptor["timing_observation_count"] == 1
        assert client.acknowledged == [str(manifest.snapshot_id)]
    finally:
        library.close()