import uuid

import pytest

from chamber_ctl.data.capture_cadence import (
    CaptureCadenceTracker,
    PulseCadenceObservation,
    decode_live_cadence,
)
from chamber_ctl.interfaces.capture_cadence_interface import CaptureCadenceChart, cadence_plot_series


class _CanvasWidget:
    def __init__(self, viewable: bool) -> None:
        self.viewable = viewable

    def winfo_viewable(self) -> bool:
        return self.viewable


class _Canvas:
    def __init__(self, viewable: bool) -> None:
        self.widget = _CanvasWidget(viewable)
        self.draw_count = 0

    def get_tk_widget(self) -> _CanvasWidget:
        return self.widget

    def draw_idle(self) -> None:
        self.draw_count += 1


def _observation(session_id: uuid.UUID, sequence: int, timestamp_ns: int) -> PulseCadenceObservation:
    return PulseCadenceObservation(session_id, sequence, timestamp_ns, timestamp_ns)


def test_plot_series_selects_requested_window_and_preserves_gap_context() -> None:
    session_id = uuid.uuid4()
    tracker = CaptureCadenceTracker()
    tracker.reset(session_id)
    tracker.set_expected_rate(100.0)
    tracker.ingest(_observation(session_id, 0, 0), received_at_monotonic_ns=0)
    tracker.ingest(_observation(session_id, 1, 30_000_000), received_at_monotonic_ns=30_000_000)
    tracker.mark_snapshot_boundary(0)
    cadence = decode_live_cadence(tracker.snapshot(100_000_000).encode(context="diagnostic"))

    series = cadence_plot_series(cadence, 2.0)

    assert len(series.relative_seconds) == 1
    assert series.gap_estimated_lost_count == (2,)
    assert series.gap_crosses_snapshot_boundary == (True,)
    assert series.provisional_indexes == (0,)
    with pytest.raises(ValueError, match="not available"):
        cadence_plot_series(cadence, 4.0)


def test_cadence_chart_draws_only_while_viewable() -> None:
    chart = object.__new__(CaptureCadenceChart)
    chart.canvas = _Canvas(viewable=False)

    chart._draw_idle_if_visible()

    assert chart.canvas.draw_count == 0
    chart.canvas.widget.viewable = True

    chart._draw_idle_if_visible()

    assert chart.canvas.draw_count == 1