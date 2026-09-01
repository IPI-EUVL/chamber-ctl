import shutil
import uuid

import numpy as np

from chamber_ctl.data.calibration import CalibrationProfile, SourceCalibrationBinding, SourceKey
from chamber_ctl.data.observer_analysis import (
    CAPTURED_ALGORITHM,
    LEGACY_COMPENSATED_ALGORITHM,
    load_observer_dose_products,
    write_observer_dose_products,
)
from chamber_ctl.subsystems.siglent_observer import ObserverCaptureRun
from euv_acquisition.models import (
    CaptureConfig,
    CapturedPulse,
    NativePulseAnalysis,
    PulseQuality,
    PulseRecord,
    SnapshotCloseReason,
    SourceBatchEnvelope,
)
from euv_acquisition.snapshot import SnapshotStore
from euv_acquisition.sources.siglent import SIGLENT_BATCH_KIND, SIGLENT_NATIVE_ANALYSIS_VERSION
from ipi_ecs.db.db_library import Library


def test_observer_products_preserve_exact_legacy_values_from_native_integrals(tmp_path) -> None:
    source_key = SourceKey("siglent", "scope-1")
    profile = CalibrationProfile(
        profile_id=uuid.uuid4(),
        revision=1,
        name="Golden Siglent profile",
        created_at=1.0,
        algorithm_version="golden-v1",
        signal_polarity=1,
        load_resistance_ohms=50.0,
        photodiode_responsivity_a_per_w=0.14,
        illuminated_area_cm2=0.05,
    )
    session_id = uuid.uuid4()
    run_id = uuid.uuid4()
    binding = SourceCalibrationBinding(
        source_key.source_kind,
        source_key.source_id,
        profile.profile_id,
        profile.revision,
    )
    run = ObserverCaptureRun(run_id, session_id, source_key, binding, 0, 0, 1_000_000_000)
    config = CaptureConfig(sample_rate_hz=1_000_000.0, window_seconds=4e-6, pretrigger_seconds=1e-6)
    persisted_integral = 4.35e-8
    records = []
    for sequence, captured_at in enumerate((0, 10_000_000_000)):
        pulse = CapturedPulse(np.zeros(4, dtype=np.float32), captured_at, captured_at)
        analysis = NativePulseAnalysis(
            baseline_volts=0.0,
            integral_volt_seconds=persisted_integral,
            minimum_volts=0.0,
            maximum_volts=3.0,
            peak_absolute_volts=3.0,
            quality=PulseQuality.OK,
            algorithm_version=SIGLENT_NATIVE_ANALYSIS_VERSION,
        )
        records.append(PulseRecord(session_id, sequence, pulse, analysis))
    envelope = SourceBatchEnvelope(uuid.uuid4(), SIGLENT_BATCH_KIND, 0, 1_000_000_000)
    source_store = SnapshotStore(tmp_path / "source")
    manifest = source_store.write(
        records,
        config,
        SnapshotCloseReason.SOURCE_BATCH,
        source_kind=source_key.source_kind,
        source_id=source_key.source_id,
        source_batch=envelope,
    )
    data_path = tmp_path / "records"
    data_path.mkdir()
    library = Library(data_path)
    entry = library.create_entry("Exposure", "Observer golden replay")
    try:
        with entry.resource(manifest.filename, "euv_snapshot", "wb") as resource:
            with source_store.path_for(manifest).open("rb") as source:
                shutil.copyfileobj(source, resource)
        context = {
            "schema_version": 1,
            "run_id": str(run_id),
            "session_id": str(session_id),
            "source_kind": source_key.source_kind,
            "source_id": source_key.source_id,
            "snapshots": [
                {
                    "snapshot_id": str(manifest.snapshot_id),
                    "capture_batch_id": str(envelope.batch_id),
                    "capture_batch_kind": envelope.batch_kind,
                    "capture_started_unix_ns": envelope.capture_started_unix_ns,
                    "capture_completed_unix_ns": envelope.capture_completed_unix_ns,
                    "is_step_exposure": {"state": "value", "value": False},
                    "exposure_start_ns": {"state": "value", "value": 0},
                    "laser_off_eligibility": {
                        "state": "eligible",
                        "evidence_count": 1,
                        "clock_bases": ["laser_status"],
                    },
                }
            ],
        }

        write_observer_dose_products(
            entry,
            data_path,
            run,
            profile,
            [manifest.snapshot_id],
            context,
            expected_native_analysis_version=SIGLENT_NATIVE_ANALYSIS_VERSION,
        )
        products = {
            product.analysis.algorithm: product
            for product in load_observer_dose_products(entry, data_path, run_id)
        }

        captured = products[CAPTURED_ALGORITHM]
        legacy = products[LEGACY_COMPENSATED_ALGORITHM]
        assert captured.analysis.total_dose_mj_cm2.hex() == "0x1.04a56280c13cdp-12"
        assert legacy.analysis.total_dose_mj_cm2.hex() == "0x1.974269e92def0p-7"
        assert legacy.graph.full.cumulative_dose_mj_cm2[-1].hex() == "0x1.974269e92def0p-7"
        assert legacy.analysis.status == "complete"
        assert legacy.analysis.completeness.included_snapshot_count == 1
        assert captured.analysis.total_dose_mj_cm2 < legacy.analysis.total_dose_mj_cm2
        assert entry.get_tags() == {}
    finally:
        library.close()