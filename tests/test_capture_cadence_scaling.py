from uuid import uuid4

import pytest

from chamber_ctl.data.capture_cadence import CaptureCadenceTracker, PulseCadenceObservation


def _ingest(tracker, session_id, sequence, timestamp_ns, *, received_ns=None):
    return tracker.ingest(
        PulseCadenceObservation(session_id, sequence, timestamp_ns, timestamp_ns),
        received_at_monotonic_ns=timestamp_ns if received_ns is None else received_ns,
    )


def test_rolling_counts_include_right_endpoint_and_exclude_left_endpoint():
    session_id = uuid4()
    tracker = CaptureCadenceTracker(rolling_windows_seconds=(1.0, 2.0, 3.0))
    tracker.reset(session_id)
    tracker.set_expected_rate(10.0)
    # Missing pulses immediately before 1 s and 2 s give one confirmed loss
    # on each boundary. Only the 2 s gap belongs in the (1 s, 2 s] window.
    timestamps = [n * 100_000_000 for n in range(21) if n not in {9, 19}]
    for sequence, timestamp in enumerate(timestamps):
        _ingest(tracker, session_id, sequence, timestamp)

    point = tracker.sample(2_000_000_000)

    assert [(w.captured_count, w.estimated_lost_count) for w in point.windows] == [(9, 1), (18, 2), (18, 2)]
    assert [w.capture_rate_hz for w in point.windows] == pytest.approx([9.0, 9.0, 9.0])


def test_delayed_reports_reconcile_multiple_rolling_windows_after_pruning():
    session_id = uuid4()
    tracker = CaptureCadenceTracker()
    tracker.reset(session_id)
    tracker.set_expected_rate(100.0)
    for sequence in range(1201):
        timestamp = sequence * 10_000_000
        _ingest(tracker, session_id, sequence, timestamp)
        if sequence % 10 == 0:
            tracker.sample(timestamp)
    assert tracker.sample(12_200_000_000).provisional_lost_count == 19
    for sequence in range(1201, 1241):
        _ingest(tracker, session_id, sequence, sequence * 10_000_000, received_ns=12_400_000_000)

    snapshot = tracker.snapshot(12_400_000_000)
    historical = next(point for point in snapshot.points if point.sampled_at_monotonic_ns == 12_200_000_000)

    assert historical.provisional_lost_count == 0
    assert [window.captured_count for window in historical.windows] == [100, 200, 300]
    assert all(window.estimated_lost_count == 0 for window in historical.windows)
    assert len(tracker._segment_observations) <= 802


def test_snapshot_boundaries_do_not_grow_with_exposure_duration():
    session_id = uuid4()
    tracker = CaptureCadenceTracker()
    tracker.reset(session_id)
    tracker.set_expected_rate(100.0)
    for sequence in range(96_001):
        _ingest(tracker, session_id, sequence, sequence * 10_000_000)
        if sequence % 250 == 249:
            tracker.mark_snapshot_boundary(sequence)
        assert len(tracker._snapshot_boundaries) <= 1

    snapshot = tracker.snapshot(960_000_000_000)

    assert snapshot.captured_count == 96_001
    assert snapshot.inferred_lost_count == 0
    assert [window.capture_rate_hz for window in snapshot.points[-1].windows] == [100.0, 100.0, 100.0]
    assert len(tracker._segment_observations) <= 802
    assert len(tracker._segment_gaps) <= 801


def test_out_of_order_future_and_late_snapshot_boundaries_still_annotate_gaps():
    session_id = uuid4()
    tracker = CaptureCadenceTracker()
    tracker.reset(session_id)
    tracker.set_expected_rate(100.0)
    _ingest(tracker, session_id, 0, 0)
    tracker.mark_snapshot_boundary(10)
    tracker.mark_snapshot_boundary(3)
    tracker.mark_snapshot_boundary(3)
    gap = _ingest(tracker, session_id, 5, 50_000_000)
    assert gap is not None and gap.crosses_snapshot_boundary
    assert tracker._snapshot_boundaries == [10]
    _ingest(tracker, session_id, 10, 100_000_000)
    gap = _ingest(tracker, session_id, 11, 110_000_000)
    assert gap is not None and gap.crosses_snapshot_boundary
    assert tracker._snapshot_boundaries == []
    tracker.mark_snapshot_boundary(5)
    snapshot = tracker.snapshot(110_000_000)
    assert next(item.gap for item in snapshot.gaps if item.gap.sequence_before == 5).crosses_snapshot_boundary
    assert tracker._snapshot_boundaries == []
