from __future__ import annotations

import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from chamber_ctl.data.capture_cadence import DEFAULT_ROLLING_WINDOW_SECONDS
from chamber_ctl.data.capture_cadence_graph import CaptureCadenceGraph


def _nearest_values(x_values: np.ndarray, y_values: np.ndarray, targets: np.ndarray) -> np.ndarray:
    if len(targets) == 0:
        return np.empty(0, dtype=np.float64)
    right = np.searchsorted(x_values, targets, side="left")
    right = np.clip(right, 0, len(x_values) - 1)
    left = np.maximum(0, right - 1)
    choose_left = np.abs(targets - x_values[left]) <= np.abs(x_values[right] - targets)
    indexes = np.where(choose_left, left, right)
    return y_values[indexes]


def build_capture_cadence_figure(
    graph: CaptureCadenceGraph,
    *,
    title: str = "Capture Integrity - Timestamp-Inferred",
) -> go.Figure:
    figure = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.10,
        row_heights=[0.56, 0.44],
    )
    figure.add_trace(
        go.Scattergl(
            x=graph.elapsed_seconds,
            y=np.full(len(graph.elapsed_seconds), graph.expected_rate_hz),
            mode="lines",
            name="Expected rate",
            line={"color": "#c9a227", "width": 1.5, "dash": "dash"},
            hovertemplate="%{x:.3f} s<br>Expected %{y:.2f} Hz<extra></extra>",
        ),
        row=1,
        col=1,
    )

    default_index = int(np.flatnonzero(graph.rolling_window_seconds == DEFAULT_ROLLING_WINDOW_SECONDS)[0])
    for index, window_seconds in enumerate(graph.rolling_window_seconds):
        visible = index == default_index
        label = f"{window_seconds:g} s"
        figure.add_trace(
            go.Scattergl(
                x=graph.elapsed_seconds,
                y=graph.capture_rate_hz[index],
                mode="lines",
                name=f"Capture rate ({label})",
                visible=visible,
                line={"color": "#168aad", "width": 2},
                hovertemplate="%{x:.3f} s<br>Captured %{y:.2f} Hz<extra></extra>",
            ),
            row=1,
            col=1,
        )
        figure.add_trace(
            go.Scattergl(
                x=graph.elapsed_seconds,
                y=graph.estimated_lost_per_second[index],
                mode="lines",
                name=f"Estimated lost ({label})",
                visible=visible,
                line={"color": "#c44536", "width": 2},
                fill="tozeroy",
                fillcolor="rgba(196,69,54,0.12)",
                hovertemplate="%{x:.3f} s<br>Estimated lost %{y:.3f}/s<extra></extra>",
            ),
            row=2,
            col=1,
        )
        gap_y = _nearest_values(
            graph.elapsed_seconds,
            graph.estimated_lost_per_second[index],
            graph.gap_elapsed_seconds,
        )
        confidence = np.where(graph.gap_confidence_high, "high", "low")
        boundary = np.where(graph.gap_crosses_snapshot_boundary, "yes", "no")
        customdata = np.column_stack(
            (
                graph.gap_estimated_lost_count,
                graph.gap_interval_seconds,
                confidence,
                boundary,
            )
        ) if len(graph.gap_elapsed_seconds) else np.empty((0, 4))
        figure.add_trace(
            go.Scatter(
                x=graph.gap_elapsed_seconds,
                y=gap_y,
                mode="markers",
                name=f"Inferred gaps ({label})",
                visible=visible,
                marker={
                    "color": np.where(graph.gap_confidence_high, "#d6a400", "#e76f51"),
                    "symbol": np.where(
                        graph.gap_crosses_snapshot_boundary,
                        "square-open",
                        "triangle-down",
                    ),
                    "size": 10,
                    "line": {"width": 1.5},
                },
                customdata=customdata,
                hovertemplate=(
                    "%{x:.3f} s<br>Estimated missing %{customdata[0]}"
                    "<br>Interval %{customdata[1]:.6f} s"
                    "<br>Confidence %{customdata[2]}"
                    "<br>Crosses snapshot boundary %{customdata[3]}<extra></extra>"
                ),
            ),
            row=2,
            col=1,
        )

    buttons = []
    for selected, window_seconds in enumerate(graph.rolling_window_seconds):
        visible = [True]
        for index in range(len(graph.rolling_window_seconds)):
            visible.extend([index == selected] * 3)
        buttons.append(
            {
                "label": f"{window_seconds:g} s",
                "method": "update",
                "args": [
                    {"visible": visible},
                    {"title": {"text": title}},
                ],
            }
        )

    summary = (
        f"{graph.quality.value.replace('_', '-')} | expected {graph.expected_rate_hz:.2f} Hz | "
        f"captured {graph.raw_capture_count:,} | estimated missing {graph.inferred_lost_count:,} | "
        f"ambiguous gaps {graph.ambiguous_gap_count:,}"
    )
    figure.update_layout(
        title={"text": title, "x": 0.02},
        template="plotly_white",
        height=760,
        margin={"l": 70, "r": 30, "t": 105, "b": 60},
        dragmode="pan",
        hovermode="x unified",
        legend={"orientation": "h", "y": 1.08, "x": 0},
        updatemenus=[
            {
                "type": "buttons",
                "direction": "right",
                "active": default_index,
                "buttons": buttons,
                "x": 1,
                "xanchor": "right",
                "y": 1.16,
                "yanchor": "top",
            }
        ],
        annotations=[
            {
                "text": summary,
                "xref": "paper",
                "yref": "paper",
                "x": 0,
                "y": 1.13,
                "showarrow": False,
                "xanchor": "left",
            }
        ],
    )
    figure.update_yaxes(title_text="Capture rate (Hz)", rangemode="tozero", row=1, col=1)
    figure.update_yaxes(title_text="Estimated lost / s", rangemode="tozero", row=2, col=1)
    figure.update_xaxes(title_text="Elapsed capture time (s)", row=2, col=1)
    return figure


def show_capture_cadence_figure(graph: CaptureCadenceGraph, *, title: str | None = None) -> None:
    figure = build_capture_cadence_figure(graph, title=title or "Capture Integrity - Timestamp-Inferred")
    figure.show(config={"scrollZoom": True, "responsive": True, "displaylogo": False})