from dataclasses import replace
import io
import uuid

import numpy as np
import pytest

from chamber_ctl.data.acquisition_preview import (
    MAX_PREVIEW_PAYLOAD_BYTES,
    PREVIEW_TRACE_LIMIT,
    AcquisitionPreview,
    build_acquisition_preview,
)
from euv_acquisition.analysis import analyze_pulse
from euv_acquisition.models import CaptureConfig, CapturedPulse, PulseRecord, SnapshotCloseReason
from euv_acquisition.snapshot import SnapshotStore


def _snapshot(tmp_path, pulse_count=20):
    config = CaptureConfig(sample_rate_hz=1_000_000.0, window_seconds=4e-6, pretrigger_seconds=1e-6)
    session_id = uuid.uuid4()
    records = []
    for sequence in range(pulse_count):
        samples = np.asarray([0.0, 0.1 + sequence / 100, 0.2, 0.0], dtype=np.float32)
        pulse = CapturedPulse(samples, 1_000 + sequence, 2_000 + sequence)
        records.append(PulseRecord(session_id, sequence, pulse, analyze_pulse(samples, config)))
    store = SnapshotStore(tmp_path)
    manifest = store.write(
        records,
        config,
        SnapshotCloseReason.EXPLICIT_FLUSH,
        source_kind="simulated",
        source_id="preview-fixture",
    )
    return store.path_for(manifest), manifest


def test_preview_round_trip_keeps_latest_full_resolution_traces(tmp_path) -> None:
    path, manifest = _snapshot(tmp_path)
    run_id = uuid.uuid4()

    preview = build_acquisition_preview(path, manifest, context="experiment", run_id=run_id)
    payload = preview.encode()
    decoded = AcquisitionPreview.decode(payload)

    assert len(payload) < MAX_PREVIEW_PAYLOAD_BYTES
    assert decoded.run_id == run_id
    assert decoded.total_pulse_count == 20
    assert decoded.included_pulse_count == PREVIEW_TRACE_LIMIT
    assert decoded.samples_v.shape == (PREVIEW_TRACE_LIMIT, 4)
    assert decoded.sequence.tolist() == list(range(4, 20))
    assert decoded.quality.dtype == np.dtype("uint32")
    assert np.array_equal(decoded.samples_v, preview.samples_v)
    pulses = decoded.to_pulses()
    assert pulses.shape == (PREVIEW_TRACE_LIMIT, 4, 2)
    assert pulses[0, 0, 0] == pytest.approx(-1e-6)


def test_preview_rejects_nonfinite_samples_and_invalid_context(tmp_path) -> None:
    path, manifest = _snapshot(tmp_path, pulse_count=1)
    preview = build_acquisition_preview(path, manifest, context="diagnostic")
    invalid_samples = preview.samples_v.copy()
    invalid_samples[0, 0] = np.nan

    with pytest.raises(ValueError, match="invalid values"):
        replace(preview, samples_v=invalid_samples)
    with pytest.raises(ValueError, match="context"):
        build_acquisition_preview(path, manifest, context="other")


def test_preview_decode_rejects_oversized_and_unknown_archive_fields() -> None:
    with pytest.raises(ValueError, match="size"):
        AcquisitionPreview.decode(b"x" * (MAX_PREVIEW_PAYLOAD_BYTES + 1))

    output = io.BytesIO()
    np.savez_compressed(output, metadata=np.asarray([], dtype=np.uint8), unexpected=np.asarray([1]))
    with pytest.raises(ValueError, match="unknown or missing"):
        AcquisitionPreview.decode(output.getvalue())