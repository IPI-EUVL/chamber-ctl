import uuid

import numpy as np

from chamber_ctl.data.capture_cadence import CadenceQuality
from chamber_ctl.data.capture_cadence_graph import CaptureCadenceGraph
from chamber_ctl.gui.capture_cadence_plot import build_capture_cadence_figure


def test_static_cadence_figure_has_window_controls_and_gap_context() -> None:
    graph = CaptureCadenceGraph(
        run_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        generated_at_unix_seconds=1.0,
        source_fingerprint="abc",
        expected_rate_hz=96.0,
        quality=CadenceQuality.TIMESTAMP_INFERRED,
        raw_capture_count=3,
        inferred_lost_count=2,
        ambiguous_gap_count=0,
        issues=(),
        elapsed_seconds=np.asarray([0.0, 0.01, 0.04]),
        source_sequence=np.asarray([0, 1, 2]),
        segment_id=np.asarray([0, 0, 0]),
        rolling_window_seconds=np.asarray([1.0, 2.0, 3.0]),
        capture_rate_hz=np.asarray([[0.0, 100.0, 50.0], [0.0, 100.0, 50.0], [0.0, 100.0, 50.0]]),
        estimated_lost_per_second=np.asarray([[0.0, 0.0, 50.0], [0.0, 0.0, 50.0], [0.0, 0.0, 50.0]]),
        gap_elapsed_seconds=np.asarray([0.04]),
        gap_sequence_before=np.asarray([1]),
        gap_sequence_after=np.asarray([2]),
        gap_interval_seconds=np.asarray([0.03]),
        gap_estimated_lost_count=np.asarray([2]),
        gap_residual_seconds=np.asarray([0.0]),
        gap_confidence_high=np.asarray([True]),
        gap_crosses_snapshot_boundary=np.asarray([True]),
    )

    figure = build_capture_cadence_figure(graph)

    assert len(figure.data) == 10
    assert figure.layout.dragmode == "pan"
    assert [button.label for button in figure.layout.updatemenus[0].buttons] == ["1 s", "2 s", "3 s"]
    assert figure.layout.updatemenus[0].active == 1
    assert figure.data[6].visible is True
    assert "snapshot boundary" in figure.data[6].hovertemplate