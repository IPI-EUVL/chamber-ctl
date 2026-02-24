import random
import time, struct, os, signal, re, sys, threading, numpy as np
import uuid
from datetime import date
from pyvisa import ResourceManager, errors as visa_errors
import socket
import csv
import queue
import math
import tkinter as tk
import numpy as np
import matplotlib
matplotlib.use("TkAgg")

from matplotlib.figure import Figure
from matplotlib import colors as mpl_colors
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.ticker import FuncFormatter

from scipy import signal
from datetime import datetime

from ipi_ecs.core.daemon import StopFlag, Daemon
from ipi_ecs.core.tcp import TCPClientSocket

from chamber_ctl.subsystems.oscilloscope import OscilloscopeStream, DummyOscilloscope

class PhosphorScopeTk:
    def __init__(
        self,
        master,
        tlim=(-100e-6, 100e-6),   # seconds
        vlim=(-1.0, 6.0),         # volts
        grid_shape=(420, 900),    # (rows, cols) for phosphor buffer
        decay=0.98,               # persistence per frame (0.90..0.99 typical)
        gain=1.0,                 # brightness per sample hit
        update_ms=20,             # redraw period
    ):
        self.master = master
        self.tmin, self.tmax = tlim
        self.vmin, self.vmax = vlim
        self.h, self.w = grid_shape
        self.decay = float(decay)
        self.gain = float(gain)
        self.update_ms = int(update_ms)

        self.buf = np.zeros((self.h, self.w), dtype=np.float32)
        self.paused = False

        self.fig = Figure(figsize=(9, 4.5), dpi=100)
        self.fig.patch.set_facecolor("black")
        self.ax = self.fig.add_subplot(111)
        self.ax.set_facecolor("black")
        self.ax.set_title("Virtual Phosphor Oscilloscope", color="white")
        self.ax.set_xlabel("Time (µs)", color="white")
        self.ax.set_ylabel("Voltage (V)", color="white")
        self.ax.tick_params(colors="white")

        self.ax.xaxis.set_major_formatter(FuncFormatter(lambda x, pos: f"{x*1e6:.0f}"))

        self._phosphor_cmap = mpl_colors.LinearSegmentedColormap.from_list(
            "phosphor_yellow",
            [
                (0.00, "#000000"),
                (0.45, "#5a4d00"),
                (0.75, "#d9b200"),
                (1.00, "#ffd400"),
            ],
        )

        self.im = self.ax.imshow(
            self.buf,
            origin="lower",
            aspect="auto",
            extent=[self.tmin, self.tmax, self.vmin, self.vmax],
            interpolation="nearest",
            cmap=self._phosphor_cmap,
            vmin=0.0,
            vmax=2.5,        # tune based on gain/decay
        )

        self.canvas = FigureCanvasTkAgg(self.fig, master=master)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)

        controls = tk.Frame(master)
        controls.pack(fill="x")

        # For demo source
        self._demo_phase = 0.0
        self._rng = np.random.default_rng(1234)

        self._schedule()

    def toggle_pause(self):
        self.paused = not self.paused

    def clear(self):
        self.buf.fill(0.0)
        self.im.set_data(self.buf)
        self.canvas.draw_idle()

    @staticmethod
    def _normalize_pulses(arr: np.ndarray) -> np.ndarray:
        """
        Normalize input to shape (P, N, 2), where [:,:,0]=t and [:,:,1]=v.
        Accepts:
          - (P, N, 2)
          - (P, 2, N)
          - (N, 2) single pulse
        """
        a = np.asarray(arr)
        if a.ndim == 2 and a.shape[1] == 2:
            return a[None, :, :]  # single pulse -> one-batch
        if a.ndim == 3 and a.shape[-1] == 2:
            return a
        if a.ndim == 3 and a.shape[1] == 2:
            return np.transpose(a, (0, 2, 1))
        raise ValueError(
            f"Unsupported pulse array shape {a.shape}. "
            "Expected (P,N,2), (P,2,N), or (N,2)."
        )

    def push(self, pulses: np.ndarray):
        """
        Add pulse batch into phosphor buffer.
        pulses shape: (P,N,2) preferred.
        """
        p = self._normalize_pulses(pulses)
        t = p[:, :, 0]
        v = p[:, :, 1]

        # Map to pixel indices
        x = ((t - self.tmin) * (self.w - 1) / (self.tmax - self.tmin)).astype(np.int32)
        y = ((v - self.vmin) * (self.h - 1) / (self.vmax - self.vmin)).astype(np.int32)

        valid = (x >= 0) & (x < self.w) & (y >= 0) & (y < self.h)
        if not np.any(valid):
            return

        flat = (y[valid] * self.w + x[valid]).ravel()
        hits = np.bincount(flat, minlength=self.h * self.w).reshape(self.h, self.w)
        self.buf += self.gain * hits.astype(np.float32)

    def _tick_render(self):
        if not self.paused:
            # Decay persistence
            self.buf *= self.decay

            # ---- DEMO DATA SOURCE ----
            # Replace this with your real pulse batch: self.push(your_batch)
            # demo_batch = self._make_demo_batch(num_pulses=12, num_points=1200)
            # self.push(demo_batch)
            # --------------------------

            # Nonlinear brightness mapping for better visibility
            display = np.sqrt(self.buf)  # quick gamma-like effect
            self.im.set_data(display)
            self.canvas.draw_idle()

    def _schedule(self):
        self._tick_render()
        self.master.after(self.update_ms, self._schedule)

    def _make_demo_batch(self, num_pulses=8, num_points=800):
        """
        Generates shape (P,N,2): synthetic jittered pulses + noise.
        """
        t = np.linspace(self.tmin, self.tmax, num_points, dtype=np.float64)
        pulses = np.empty((num_pulses, num_points, 2), dtype=np.float64)

        for i in range(num_pulses):
            # Jittered pulse center + width + amplitude
            center = 20e-9 * np.sin(self._demo_phase) + self._rng.normal(0, 1.5e-9)
            width = 5e-9 + self._rng.uniform(-1e-9, 1e-9)
            amp = 4.8 + self._rng.normal(0, 0.05)

            pulse = amp * np.exp(-0.5 * ((t - center) / width) ** 2)
            baseline = 0.05 * self._rng.normal(size=num_points)
            v = pulse + baseline

            pulses[i, :, 0] = t
            pulses[i, :, 1] = v
            self._demo_phase += 0.045

        return pulses
    
def __update_thread(phosphor : PhosphorScopeTk, scope: OscilloscopeStream, stop_flag: StopFlag):
    print("Starting update thread...")

    while stop_flag.run():
        timestamp, data, indexes, uid = scope.get_out_queue().get() # wait for signal of new data

        while not scope.get_out_queue().empty():
            timestamp, data, indexes, uid = scope.get_out_queue().get() # get latest data, discard older ones
        
        indexes = np.array(indexes)
        data = np.array(data)

        pulse_size = int(indexes[1, 0] - indexes[0, 0])
        pulses = np.reshape(data, (-1, pulse_size, 2))

        last_time = indexes[0, 1]
        for n, pulse in enumerate(pulses):
            cur_time = indexes[n, 1]
            time.sleep(max(0, cur_time - last_time))
            last_time = cur_time

            phosphor.push([pulse])


if __name__ == "__main__":
    root = tk.Tk()
    root.title("TkAgg Phosphor Pulse Overlay Demo")

    phosphor = PhosphorScopeTk(
        root,
        tlim=(-0e-6, 10e-6),
        vlim=(-0.5, 1.0),
        grid_shape=(200, 500),
        decay=0.98,
        gain=0.8,
        update_ms=20,
    )

    scope = DummyOscilloscope()

    daemon = Daemon()
    daemon.add(__update_thread, phosphor=phosphor, scope=scope)
    print("Starting daemon...")
    daemon.start()
    print("Daemon started.")

    try:
        scope.start()
        root.mainloop()
    finally:
        scope.close()
        daemon.stop()