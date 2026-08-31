from __future__ import annotations

import math
import tkinter as tk
from dataclasses import dataclass

import matplotlib

matplotlib.use("TkAgg")

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

from chamber_ctl.data.capture_cadence import DecodedLiveCadence


@dataclass(frozen=True)
class CadencePlotSeries:
    relative_seconds: tuple[float, ...]
    capture_rate_hz: tuple[float, ...]
    expected_rate_hz: tuple[float, ...]
    estimated_lost_per_second: tuple[float, ...]
    provisional_indexes: tuple[int, ...]
    gap_relative_seconds: tuple[float, ...]
    gap_estimated_lost_count: tuple[int, ...]
    gap_crosses_snapshot_boundary: tuple[bool, ...]


def cadence_plot_series(cadence: DecodedLiveCadence, window_seconds: float) -> CadencePlotSeries:
    if window_seconds not in cadence.rolling_window_options_seconds:
        raise ValueError("Selected cadence window is not available.")
    capture_rate = []
    expected_rate = []
    lost_rate = []
    provisional_indexes = []
    for index, point in enumerate(cadence.points):
        window = next(item for item in point.windows if item.window_seconds == window_seconds)
        capture_rate.append(math.nan if window.capture_rate_hz is None else window.capture_rate_hz)
        expected_rate.append(math.nan if point.expected_rate_hz is None else point.expected_rate_hz)
        lost_rate.append(
            math.nan
            if window.estimated_lost_per_second is None
            else window.estimated_lost_per_second
        )
        if point.provisional_lost_count > 0:
            provisional_indexes.append(index)
    return CadencePlotSeries(
        relative_seconds=tuple(point.relative_seconds for point in cadence.points),
        capture_rate_hz=tuple(capture_rate),
        expected_rate_hz=tuple(expected_rate),
        estimated_lost_per_second=tuple(lost_rate),
        provisional_indexes=tuple(provisional_indexes),
        gap_relative_seconds=tuple(gap.relative_seconds for gap in cadence.gaps),
        gap_estimated_lost_count=tuple(gap.estimated_lost_count for gap in cadence.gaps),
        gap_crosses_snapshot_boundary=tuple(gap.crosses_snapshot_boundary for gap in cadence.gaps),
    )


def _finite_max(values: tuple[float, ...], default: float) -> float:
    finite = [value for value in values if math.isfinite(value)]
    return max(finite, default=default)


class CaptureCadenceChart:
    def __init__(self, master) -> None:
        self._closed = False
        self._cadence: DecodedLiveCadence | None = None
        self._window_seconds = 2.0
        self._gap_labels = []

        self.figure = Figure(figsize=(6.0, 4.5), dpi=100, constrained_layout=True)
        self.figure.patch.set_facecolor("#11161b")
        self.capture_axis = self.figure.add_subplot(211)
        self.loss_axis = self.figure.add_subplot(212, sharex=self.capture_axis)
        for axis in (self.capture_axis, self.loss_axis):
            axis.set_facecolor("#11161b")
            axis.tick_params(colors="#d6dde3", labelsize=8)
            axis.grid(True, color="#3c4852", alpha=0.45, linewidth=0.6)
            for spine in axis.spines.values():
                spine.set_color("#5e6b75")
        self.capture_axis.set_ylabel("Capture (Hz)", color="#d6dde3")
        self.loss_axis.set_ylabel("Estimated lost/s", color="#d6dde3")
        self.loss_axis.set_xlabel("Seconds from latest sample", color="#d6dde3")

        (self._capture_line,) = self.capture_axis.plot(
            [], [], color="#53c7df", linewidth=1.8, label="Captured"
        )
        (self._expected_line,) = self.capture_axis.plot(
            [], [], color="#d9d16f", linewidth=1.2, linestyle="--", label="Expected"
        )
        (self._loss_line,) = self.loss_axis.plot(
            [], [], color="#df6b62", linewidth=1.8, label="Estimated loss"
        )
        self._provisional_markers = self.loss_axis.scatter(
            [], [], color="#f0a84f", marker="o", s=24, zorder=4, label="Provisional"
        )
        self._gap_markers = self.loss_axis.scatter(
            [], [], color="#f5d76e", marker="v", s=40, zorder=5, label="Confirmed gap"
        )
        self._boundary_markers = self.loss_axis.scatter(
            [], [], facecolors="none", edgecolors="#f5d76e", marker="s", s=65, zorder=6,
            label="Snapshot boundary",
        )
        self.capture_axis.legend(loc="upper left", fontsize=7, frameon=False, labelcolor="#d6dde3")
        self.loss_axis.legend(loc="upper left", fontsize=7, frameon=False, labelcolor="#d6dde3")
        self.capture_axis.tick_params(labelbottom=False)

        self.canvas = FigureCanvasTkAgg(self.figure, master=master)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        self.clear()

    @property
    def window_seconds(self) -> float:
        return self._window_seconds

    def set_window(self, window_seconds: float) -> None:
        selected = float(window_seconds)
        if self._cadence is not None and selected not in self._cadence.rolling_window_options_seconds:
            raise ValueError("Selected cadence window is not available.")
        self._window_seconds = selected
        if self._cadence is not None:
            self.update(self._cadence)

    def update(self, cadence: DecodedLiveCadence) -> None:
        if self._closed:
            return
        self._cadence = cadence
        if self._window_seconds not in cadence.rolling_window_options_seconds:
            self._window_seconds = cadence.default_rolling_window_seconds
        series = cadence_plot_series(cadence, self._window_seconds)
        self._capture_line.set_data(series.relative_seconds, series.capture_rate_hz)
        self._expected_line.set_data(series.relative_seconds, series.expected_rate_hz)
        self._loss_line.set_data(series.relative_seconds, series.estimated_lost_per_second)

        provisional_offsets = [
            (series.relative_seconds[index], series.estimated_lost_per_second[index])
            for index in series.provisional_indexes
            if math.isfinite(series.estimated_lost_per_second[index])
        ]
        self._provisional_markers.set_offsets(provisional_offsets or [[math.nan, math.nan]])

        gap_offsets = []
        boundary_offsets = []
        for relative_seconds, count, boundary in zip(
            series.gap_relative_seconds,
            series.gap_estimated_lost_count,
            series.gap_crosses_snapshot_boundary,
        ):
            y_value = self._loss_value_at(series, relative_seconds)
            gap_offsets.append((relative_seconds, y_value))
            if boundary:
                boundary_offsets.append((relative_seconds, y_value))
        self._gap_markers.set_offsets(gap_offsets or [[math.nan, math.nan]])
        self._boundary_markers.set_offsets(boundary_offsets or [[math.nan, math.nan]])

        for label in self._gap_labels:
            label.remove()
        self._gap_labels.clear()
        for (x_value, y_value), count, boundary in zip(
            gap_offsets,
            series.gap_estimated_lost_count,
            series.gap_crosses_snapshot_boundary,
        ):
            suffix = " / snapshot" if boundary else ""
            self._gap_labels.append(
                self.loss_axis.annotate(
                    f"-{count}{suffix}",
                    (x_value, y_value),
                    xytext=(3, 6),
                    textcoords="offset points",
                    color="#f5d76e",
                    fontsize=7,
                )
            )

        horizon = cadence.display_horizon_seconds
        self.capture_axis.set_xlim(-horizon, 0.0)
        capture_max = _finite_max(series.capture_rate_hz + series.expected_rate_hz, 1.0)
        loss_max = _finite_max(series.estimated_lost_per_second, 1.0)
        self.capture_axis.set_ylim(0.0, capture_max * 1.12)
        self.loss_axis.set_ylim(0.0, loss_max * 1.2)
        self.canvas.draw_idle()

    @staticmethod
    def _loss_value_at(series: CadencePlotSeries, relative_seconds: float) -> float:
        candidates = [
            (abs(x_value - relative_seconds), y_value)
            for x_value, y_value in zip(series.relative_seconds, series.estimated_lost_per_second)
            if math.isfinite(y_value)
        ]
        return min(candidates, default=(0.0, 0.0))[1]

    def clear(self) -> None:
        if self._closed:
            return
        self._cadence = None
        for line in (self._capture_line, self._expected_line, self._loss_line):
            line.set_data([], [])
        for markers in (self._provisional_markers, self._gap_markers, self._boundary_markers):
            markers.set_offsets([[math.nan, math.nan]])
        for label in self._gap_labels:
            label.remove()
        self._gap_labels.clear()
        self.capture_axis.set_xlim(-5.0, 0.0)
        self.capture_axis.set_ylim(0.0, 1.0)
        self.loss_axis.set_ylim(0.0, 1.0)
        self.canvas.draw_idle()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.figure.clear()