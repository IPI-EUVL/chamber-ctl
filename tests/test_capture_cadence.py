import uuid

import pytest

from chamber_ctl.data.capture_cadence import (
    CadenceQuality,
    CaptureCadenceTracker,
    GapConfidence,
    MAX_CADENCE_PAYLOAD_BYTES,
    PulseCadenceObservation,
    decode_live_cadence,
    infer_gap,
    infer_gaps,
)


PERIOD_NS = 10_416_667


def _observation(
    session_id: uuid.UUID,
    sequence: int,
    monotonic_ns: int,
    *,
    ordinal: int | None = None,
) -> PulseCadenceObservation:
    return PulseCadenceObservation(
        session_id=session_id,
        sequence=sequence,
        captured_at_unix_ns=1_000_000_000 + monotonic_ns,
        captured_at_monotonic_ns=monotonic_ns,
        trigger_ordinal=ordinal,
        counter_epoch="counter-a" if ordinal is not None else None,
    )


def test_infer_gaps_reports_steady_cadence_and_known_missing_periods() -> None:
    session_id = uuid.uuid4()
    observations = (
        _observation(session_id, 0, 0),
        _observation(session_id, 1, PERIOD_NS),
        _observation(session_id, 2, PERIOD_NS * 4),
    )

    gaps = infer_gaps(observations, 96.0)

    assert [gap.estimated_lost_count for gap in gaps] == [0, 2]
    assert all(gap.quality is CadenceQuality.TIMESTAMP_INFERRED for gap in gaps)
    assert all(gap.confidence is GapConfidence.HIGH for gap in gaps)


def test_counter_ordinals_override_timestamp_inference_when_available() -> None:
    session_id = uuid.uuid4()
    previous = _observation(session_id, 10, 0, ordinal=100)
    current = _observation(session_id, 11, PERIOD_NS, ordinal=104)

    gap = infer_gap(previous, current, 96.0)

    assert gap.estimated_lost_count == 3
    assert gap.quality is CadenceQuality.COUNTER_EXACT


def test_large_timing_residual_is_retained_but_marked_low_confidence() -> None:
    session_id = uuid.uuid4()

    gap = infer_gap(
        _observation(session_id, 0, 0),
        _observation(session_id, 1, 14_000_000),
        96.0,
    )

    assert gap.estimated_lost_count == 0
    assert gap.confidence is GapConfidence.LOW


def test_live_tracker_reports_steady_rate_and_provisional_loss() -> None:
    session_id = uuid.uuid4()
    tracker = CaptureCadenceTracker()
    tracker.reset(session_id)
    tracker.set_expected_rate(100.0)
    for sequence in range(201):
        timestamp = sequence * 10_000_000
        tracker.ingest(
            _observation(session_id, sequence, timestamp),
            received_at_monotonic_ns=timestamp,
        )

    steady = tracker.snapshot(2_000_000_000)
    two_second = next(item for item in steady.points[-1].windows if item.window_seconds == 2.0)
    assert two_second.capture_rate_hz == pytest.approx(100.0)
    assert two_second.estimated_lost_per_second == 0.0

    silent = tracker.snapshot(2_035_000_000)
    two_second = next(item for item in silent.points[-1].windows if item.window_seconds == 2.0)
    assert silent.points[-1].provisional_lost_count == 3
    assert two_second.estimated_lost_per_second == pytest.approx(1.5)


def test_timing_transition_starts_a_new_segment_without_counting_disabled_gap() -> None:
    session_id = uuid.uuid4()
    tracker = CaptureCadenceTracker()
    tracker.reset(session_id)
    tracker.set_expected_rate(100.0)
    tracker.ingest(_observation(session_id, 0, 0), received_at_monotonic_ns=0)
    tracker.ingest(_observation(session_id, 1, 10_000_000), received_at_monotonic_ns=10_000_000)

    tracker.set_expected_rate(None)
    tracker.set_expected_rate(100.0)
    tracker.ingest(_observation(session_id, 2, 1_000_000_000), received_at_monotonic_ns=1_000_000_000)

    snapshot = tracker.snapshot(1_000_000_000)
    assert snapshot.inferred_lost_count == 0
    assert snapshot.gaps[-1].gap.sequence_after == 1


def test_live_snapshot_keeps_only_the_five_second_display_horizon() -> None:
    session_id = uuid.uuid4()
    tracker = CaptureCadenceTracker(sample_interval_seconds=0.1)
    tracker.reset(session_id)
    tracker.set_expected_rate(10.0)
    for sample_index in range(81):
        now_ns = sample_index * 100_000_000
        tracker.ingest(
            _observation(session_id, sample_index, now_ns),
            received_at_monotonic_ns=now_ns,
        )
        tracker.sample(now_ns)

    snapshot = tracker.snapshot(8_000_000_000)

    assert len(snapshot.points) == 51
    assert snapshot.points[0].sampled_at_monotonic_ns == 3_000_000_000
    assert snapshot.points[-1].sampled_at_monotonic_ns == 8_000_000_000


def test_live_payload_round_trips_relative_points_and_rejects_unknown_fields() -> None:
    session_id = uuid.uuid4()
    tracker = CaptureCadenceTracker()
    tracker.reset(session_id)
    tracker.set_expected_rate(100.0)
    tracker.ingest(_observation(session_id, 0, 0), received_at_monotonic_ns=0)
    tracker.ingest(_observation(session_id, 1, 30_000_000), received_at_monotonic_ns=30_000_000)
    snapshot = tracker.snapshot(100_000_000)

    run_id = uuid.uuid4()
    decoded = decode_live_cadence(snapshot.encode(context="exposure", run_id=run_id))

    assert decoded.context == "exposure"
    assert decoded.run_id == run_id
    assert decoded.session_id == session_id
    assert decoded.points[-1].relative_seconds == 0.0
    assert decoded.gaps[0].estimated_lost_count == 2
    invalid = snapshot.encode().replace(b'"schema_version":2', b'"schema_version":999')
    with pytest.raises(ValueError, match="Unsupported"):
        decode_live_cadence(invalid)


def test_snapshot_boundary_marks_an_already_inferred_gap() -> None:
    session_id = uuid.uuid4()
    tracker = CaptureCadenceTracker()
    tracker.reset(session_id)
    tracker.set_expected_rate(100.0)
    tracker.ingest(_observation(session_id, 10, 0), received_at_monotonic_ns=0)
    tracker.ingest(_observation(session_id, 11, 30_000_000), received_at_monotonic_ns=30_000_000)

    tracker.mark_snapshot_boundary(10)
    decoded = decode_live_cadence(tracker.snapshot(30_000_000).encode())

    assert decoded.gaps[0].crosses_snapshot_boundary is True


def test_disabling_inference_retains_expected_rate_and_evidence_quality() -> None:
    session_id = uuid.uuid4()
    tracker = CaptureCadenceTracker()
    tracker.reset(session_id)
    tracker.set_expected_rate(96.0)
    tracker.ingest(_observation(session_id, 0, 0), received_at_monotonic_ns=0)

    tracker.set_expected_rate(None)
    decoded = decode_live_cadence(tracker.snapshot(10_000_000).encode())

    assert decoded.expected_rate_hz == 96.0
    assert decoded.quality is CadenceQuality.TIMESTAMP_INFERRED


def test_dense_live_gaps_are_bounded_for_dds_while_totals_are_preserved() -> None:
    session_id = uuid.uuid4()
    tracker = CaptureCadenceTracker()
    tracker.reset(session_id)
    tracker.set_expected_rate(96.0)
    for sequence in range(319):
        timestamp_ns = sequence * 20_000_000
        tracker.ingest(
            _observation(session_id, sequence, timestamp_ns),
            received_at_monotonic_ns=timestamp_ns,
        )
        tracker.snapshot(timestamp_ns)

    payload = tracker.snapshot(6_380_000_000).encode(context="diagnostic")
    decoded = decode_live_cadence(payload)

    assert len(payload) <= MAX_CADENCE_PAYLOAD_BYTES < 65_536
    assert decoded.inferred_lost_count == 318
    assert decoded.omitted_gap_count > 0
    assert len(decoded.gaps) + decoded.omitted_gap_count == 250