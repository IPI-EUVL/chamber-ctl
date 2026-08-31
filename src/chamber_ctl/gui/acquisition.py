from __future__ import annotations

import json
import math
import queue
import threading
import time
import tkinter as tk
import uuid
from collections import deque
from dataclasses import dataclass
from tkinter import messagebox, ttk

from ipi_ecs.dds import client, magics, subsystem as dds_subsystem, types
from ipi_ecs.subsystems.experiment_controller import ExperimentReader

from chamber_ctl import ECS_IP
from chamber_ctl.data.acquisition_preview import AcquisitionPreview
from chamber_ctl.data.capture_cadence import DecodedLiveCadence, decode_live_cadence
from chamber_ctl.data.capture_cadence_graph import ensure_capture_cadence_graph
from chamber_ctl.gui.capture_cadence_plot import show_capture_cadence_figure
from chamber_ctl.interfaces.capture_cadence_interface import CaptureCadenceChart
from chamber_ctl.interfaces.scope_interface import PhosphorScopeTk
from chamber_ctl.subsystems import uuids


DIAGNOSTIC_EVENTS = {
    "start": b"acquisition_test_start",
    "one_shot": b"acquisition_test_one_shot",
    "flush": b"acquisition_test_flush",
    "stop": b"acquisition_test_stop",
}
SIMULATOR_SET_EVENT = b"set_acquisition_simulator_control"
SIMULATOR_RESTORE_EVENT = b"restore_acquisition_simulator_controls"
AUTHORIZE_RECOVERY_EVENT = b"resume_acquisition_interlock"
RECOVER_ORPHAN_EVENT = b"recover_orphaned_capture_session"


@dataclass(frozen=True)
class AcquisitionControlState:
    start_enabled: bool
    one_shot_enabled: bool
    flush_enabled: bool
    stop_enabled: bool
    simulator_enabled: bool
    authorize_recovery_enabled: bool
    recover_orphan_enabled: bool


@dataclass(frozen=True)
class AcquisitionPipelineStatus:
    mode: str
    fallback: str
    accepted_rate: str
    queues: str
    timings: str
    fault: str


def decode_acquisition_status(payload) -> dict:
    raw = bytes(payload) if isinstance(payload, list) else payload
    if not isinstance(raw, bytes):
        raise ValueError("Acquisition status must be bytes.")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("state"), str):
        raise ValueError("Acquisition status must be a JSON object with a state.")
    return value


def acquisition_control_state(status: dict | None, *, dds_connected: bool) -> AcquisitionControlState:
    if not dds_connected or not isinstance(status, dict):
        return AcquisitionControlState(False, False, False, False, False, False, False)
    state = status.get("state")
    capture_connected = status.get("capture_connected") is True
    idle = state == "idle" and capture_connected
    diagnostic_running = state == "diagnostic_running"
    diagnostic_mode = status.get("diagnostic_mode")
    simulator_enabled = (
        capture_connected
        and status.get("source_kind") == "simulated"
        and isinstance(status.get("capabilities"), dict)
        and status["capabilities"].get("simulator_controls") is True
    )
    return AcquisitionControlState(
        start_enabled=idle,
        one_shot_enabled=idle,
        flush_enabled=diagnostic_running and diagnostic_mode == "continuous",
        stop_enabled=state in {"diagnostic_running", "diagnostic_error"},
        simulator_enabled=simulator_enabled,
        authorize_recovery_enabled=(
            state == "running"
            and status.get("pulse_loss") is True
            and status.get("recovery_ready") is True
            and status.get("resume_authorized") is not True
        ),
        recover_orphan_enabled=state == "recovery_required" or (state == "idle" and capture_connected),
    )


def acquisition_status_metrics(status: dict | None) -> tuple[str, str]:
    if not isinstance(status, dict):
        return "N/A", "N/A"

    def format_value(key: str, unit: str) -> str:
        value = status.get(key)
        if isinstance(value, bool):
            return "N/A"
        try:
            number = float(value)
        except (TypeError, ValueError):
            return "N/A"
        if not math.isfinite(number):
            return "N/A"
        return f"{number:.2f} {unit}"

    return (
        format_value("accumulated_dose_mj_cm2", "mJ/cm2"),
        format_value("transmitting_runtime_seconds", "s"),
    )


def acquisition_pipeline_status(status: dict | None) -> AcquisitionPipelineStatus:
    unavailable = AcquisitionPipelineStatus("N/A", "N/A", "N/A", "N/A", "N/A", "N/A")
    if not isinstance(status, dict):
        return unavailable
    metrics = status.get("pipeline_metrics")
    if not isinstance(metrics, dict):
        return unavailable

    capture_mode = metrics.get("capture_mode")
    if isinstance(capture_mode, dict):
        requested = capture_mode.get("requested")
        effective = capture_mode.get("effective")
        fallback_reason = capture_mode.get("fallback_reason")
    else:
        requested = effective = fallback_reason = None
    requested = requested.strip() if isinstance(requested, str) and requested.strip() else None
    effective = effective.strip() if isinstance(effective, str) and effective.strip() else None
    fallback_reason = (
        fallback_reason.strip()
        if isinstance(fallback_reason, str) and fallback_reason.strip()
        else None
    )
    mode = effective or requested or "N/A"
    if requested is not None and effective is not None and requested != effective:
        mode = f"{effective} (requested {requested})"

    counters = metrics.get("counters")
    accepted = counters.get("accepted") if isinstance(counters, dict) else None
    elapsed_seconds = metrics.get("elapsed_seconds")
    accepted_rate = "N/A"
    if (
        isinstance(accepted, int)
        and not isinstance(accepted, bool)
        and accepted >= 0
        and isinstance(elapsed_seconds, (int, float))
        and not isinstance(elapsed_seconds, bool)
        and math.isfinite(elapsed_seconds)
        and elapsed_seconds > 0
    ):
        accepted_rate = f"{accepted / elapsed_seconds:.1f} Hz ({accepted} total)"

    queues = metrics.get("queues")
    queue_values = []
    if isinstance(queues, dict):
        ordered_names = [name for name in ("capture", "persistence", "control") if name in queues]
        ordered_names.extend(sorted(set(queues) - set(ordered_names)))
        for name in ordered_names:
            value = queues.get(name)
            if not isinstance(value, dict):
                continue
            depth = value.get("depth")
            capacity = value.get("capacity")
            high_water = value.get("high_water")
            if all(isinstance(item, int) and not isinstance(item, bool) for item in (depth, capacity, high_water)):
                queue_values.append(f"{name} {depth}/{capacity} (high {high_water})")

    stages = metrics.get("stages")
    timing_values = []
    timing_names = (
        ("hardware_read", "read"),
        ("capture_queue_wait", "capture wait"),
        ("analysis", "analysis"),
        ("snapshot_write", "write"),
        ("trigger_to_report", "trigger-report"),
    )
    if isinstance(stages, dict):
        for stage_name, label in timing_names:
            stage = stages.get(stage_name)
            p95_ms = stage.get("p95_ms") if isinstance(stage, dict) else None
            if (
                isinstance(p95_ms, (int, float))
                and not isinstance(p95_ms, bool)
                and math.isfinite(p95_ms)
                and p95_ms >= 0
            ):
                timing_values.append(f"{label} {p95_ms:.2f} ms")

    terminal_error = metrics.get("terminal_error")
    fault = terminal_error.strip() if isinstance(terminal_error, str) and terminal_error.strip() else "none"
    return AcquisitionPipelineStatus(
        mode=mode,
        fallback=fallback_reason or "none",
        accepted_rate=accepted_rate,
        queues=" | ".join(queue_values) or "N/A",
        timings=" | ".join(timing_values) or "N/A",
        fault=fault,
    )


def acquisition_status_detail(status: dict) -> str:
    state = str(status.get("state", "unknown"))
    if state.startswith("diagnostic_"):
        mode = str(status.get("diagnostic_mode", "unknown")).replace("_", "-")
        reports = int(status.get("diagnostic_report_count", 0))
        transferred = int(status.get("processed_snapshot_count", 0))
        pending = int(status.get("pending_snapshot_count", 0))
        detail = (
            f"{mode}; {reports} pulse report(s); {transferred} snapshot(s) transferred; "
            f"{pending} pending"
        )
        error = status.get("diagnostic_error")
        return detail if not error else f"{detail}; {error}"
    if state in {"running", "finalizing"}:
        sequence = status.get("last_sequence")
        return "Exposure capture" + ("; no pulse reports" if sequence is None else f"; sequence {sequence}")

    detail = status.get("finalization_detail") or status.get("diagnostic_error")
    if detail:
        return str(detail)
    last_diagnostic = status.get("last_diagnostic")
    if isinstance(last_diagnostic, dict):
        mode = str(last_diagnostic.get("mode", "unknown")).replace("_", "-")
        reports = int(last_diagnostic.get("report_count", 0))
        snapshots = int(last_diagnostic.get("snapshot_count", 0))
        return f"Last {mode} test: {reports} pulse report(s); {snapshots} snapshot(s) transferred"
    return "Ready for acquisition."


class AcquisitionPreviewHistory:
    def __init__(self, limit: int = 20) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("Preview history limit must be a positive integer.")
        self._items: deque[AcquisitionPreview] = deque(maxlen=limit)
        self._session_id: uuid.UUID | None = None

    @property
    def session_id(self) -> uuid.UUID | None:
        return self._session_id

    def append(self, preview: AcquisitionPreview) -> int:
        if preview.session_id != self._session_id:
            self._items.clear()
            self._session_id = preview.session_id
        self._items.append(preview)
        return len(self._items) - 1

    def __len__(self) -> int:
        return len(self._items)

    def __getitem__(self, index: int) -> AcquisitionPreview:
        return self._items[index]


@dataclass
class _PendingEvent:
    action: str
    label: str
    handle: object
    deadline: float
    last_feedback: str | None = None


class AcquisitionGUI:
    HISTORY_LIMIT = 20
    UI_UPDATE_MS = 100

    def __init__(
        self,
        root,
        own_window: bool = True,
        dds_ip: str = ECS_IP,
        data_path: str | None = None,
    ) -> None:
        self.__root = root
        self.__own_window = own_window
        self.__data_path = data_path
        self.__run = True
        self.__dds_connected = False
        self.__status: dict | None = None
        self.__history = AcquisitionPreviewHistory(self.HISTORY_LIMIT)
        self.__history_index = -1
        self.__pending_actions: set[str] = set()
        self.__ui_queue: queue.Queue = queue.Queue()
        self.__work_queue: queue.Queue = queue.Queue()
        self.__stop_event = threading.Event()
        self.__events: dict[bytes, object] = {}
        self.__client = None
        self.__subsystem = None
        self.__status_kv = None
        self.__preview_kv = None
        self.__cadence_kv = None
        self.__ui_job = None
        self.__cadence: DecodedLiveCadence | None = None
        self.__cadence_plot_pending = False

        self.__connection_text = tk.StringVar(value="DDS: connecting")
        self.__state_text = tk.StringVar(value="Acquisition: unavailable")
        self.__source_text = tk.StringVar(value="Source: unavailable")
        self.__detail_text = tk.StringVar(value="Waiting for acquisition status.")
        self.__dose_text = tk.StringVar(value="Dose: N/A")
        self.__runtime_text = tk.StringVar(value="Transmitting time: N/A")
        self.__pipeline_mode_text = tk.StringVar(value="Mode: N/A")
        self.__pipeline_rate_text = tk.StringVar(value="Accepted: N/A")
        self.__pipeline_queues_text = tk.StringVar(value="Queues: N/A")
        self.__pipeline_timings_text = tk.StringVar(value="p95: N/A")
        self.__pipeline_fallback_text = tk.StringVar(value="Fallback: N/A")
        self.__pipeline_fault_text = tk.StringVar(value="Fault: N/A")
        self.__cadence_rate_text = tk.StringVar(value="Capture rate: N/A")
        self.__cadence_loss_text = tk.StringVar(value="Estimated lost: N/A")
        self.__cadence_total_text = tk.StringVar(value="Estimated missing: 0")
        self.__cadence_quality_text = tk.StringVar(value="Evidence: unavailable")
        self.__cadence_window = tk.IntVar(value=2)
        self.__preview_text = tk.StringVar(value="No waveform preview received.")
        self.__history_text = tk.StringVar(value="0 / 0")
        self.__result_text = tk.StringVar(value="No acquisition action requested.")
        self.__laser_enabled = tk.BooleanVar(value=True)
        self.__chopper_enabled = tk.BooleanVar(value=True)
        self.__pll_locked = tk.BooleanVar(value=True)

        self.__build_ui()
        self.__worker = threading.Thread(target=self.__worker_loop, name="acquisition-gui-worker", daemon=True)
        self.__worker.start()
        self.__setup_client(dds_ip)
        self.__ui_job = self.__root.after(self.UI_UPDATE_MS, self.__ui_tick)

    def __build_ui(self) -> None:
        if self.__own_window and hasattr(self.__root, "title"):
            self.__root.title("EUV Acquisition")
        if self.__own_window and hasattr(self.__root, "geometry"):
            self.__root.geometry("1400x800")

        outer = ttk.Frame(self.__root, padding=10)
        outer.pack(fill=tk.BOTH, expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)

        status = ttk.Frame(outer)
        status.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        status.columnconfigure(1, weight=1)
        ttk.Label(status, text="EUV Acquisition", font=("Segoe UI", 16, "bold")).grid(
            row=0, column=0, rowspan=2, sticky="w", padx=(0, 20)
        )
        ttk.Label(status, textvariable=self.__state_text, font=("Segoe UI", 11, "bold")).grid(
            row=0, column=1, sticky="w"
        )
        ttk.Label(status, textvariable=self.__detail_text).grid(row=1, column=1, sticky="w")
        ttk.Label(status, textvariable=self.__connection_text).grid(row=0, column=2, sticky="e")
        ttk.Label(status, textvariable=self.__source_text).grid(row=1, column=2, sticky="e")
        metrics = ttk.Frame(status)
        metrics.grid(row=2, column=1, columnspan=2, sticky="ew", pady=(3, 0))
        ttk.Label(metrics, textvariable=self.__dose_text).pack(side=tk.LEFT, padx=(0, 18))
        ttk.Label(metrics, textvariable=self.__runtime_text).pack(side=tk.LEFT)

        pipeline = ttk.LabelFrame(status, text="Pipeline", padding=(6, 3))
        pipeline.grid(row=3, column=1, columnspan=2, sticky="ew", pady=(4, 0))
        pipeline.columnconfigure(2, weight=1)
        ttk.Label(pipeline, textvariable=self.__pipeline_mode_text).grid(row=0, column=0, sticky="w", padx=(0, 16))
        ttk.Label(pipeline, textvariable=self.__pipeline_rate_text).grid(row=0, column=1, sticky="w", padx=(0, 16))
        ttk.Label(pipeline, textvariable=self.__pipeline_queues_text).grid(row=0, column=2, sticky="w")
        ttk.Label(pipeline, textvariable=self.__pipeline_timings_text).grid(row=1, column=0, columnspan=3, sticky="w")
        ttk.Label(pipeline, textvariable=self.__pipeline_fallback_text).grid(row=2, column=0, columnspan=2, sticky="w", padx=(0, 16))
        ttk.Label(pipeline, textvariable=self.__pipeline_fault_text).grid(row=2, column=2, sticky="w")

        live_panes = ttk.Panedwindow(outer, orient=tk.HORIZONTAL)
        live_panes.grid(row=1, column=0, sticky="nsew")

        cadence = ttk.LabelFrame(live_panes, text="Capture Integrity (timestamp-inferred)", padding=6)
        cadence.columnconfigure(0, weight=1)
        cadence.rowconfigure(1, weight=1)
        live_panes.add(cadence, weight=1)

        cadence_bar = ttk.Frame(cadence)
        cadence_bar.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        cadence_bar.columnconfigure(0, weight=1)
        cadence_bar.columnconfigure(1, weight=1)
        ttk.Label(cadence_bar, textvariable=self.__cadence_rate_text).grid(row=0, column=0, sticky="w")
        ttk.Label(cadence_bar, textvariable=self.__cadence_loss_text).grid(row=0, column=1, sticky="w")
        ttk.Label(cadence_bar, textvariable=self.__cadence_total_text).grid(row=1, column=0, sticky="w")
        ttk.Label(cadence_bar, textvariable=self.__cadence_quality_text).grid(row=1, column=1, sticky="w")
        window_selector = ttk.Frame(cadence_bar)
        window_selector.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(3, 0))
        ttk.Label(window_selector, text="Rolling window:").pack(side=tk.LEFT, padx=(0, 3))
        for seconds in (1, 2, 3):
            ttk.Radiobutton(
                window_selector,
                text=f"{seconds} s",
                value=seconds,
                variable=self.__cadence_window,
                command=self.__select_cadence_window,
            ).pack(side=tk.LEFT)
        self.__open_cadence_button = ttk.Button(
            window_selector,
            text="Open Last Exposure",
            command=self.__open_last_cadence,
        )
        self.__open_cadence_button.pack(side=tk.RIGHT)

        cadence_host = ttk.Frame(cadence)
        cadence_host.grid(row=1, column=0, sticky="nsew")
        self.__cadence_chart = CaptureCadenceChart(cadence_host)

        waveform = ttk.LabelFrame(live_panes, text="Live Pulse Windows", padding=6)
        waveform.columnconfigure(0, weight=1)
        waveform.rowconfigure(1, weight=1)
        live_panes.add(waveform, weight=1)

        waveform_bar = ttk.Frame(waveform)
        waveform_bar.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        waveform_bar.columnconfigure(0, weight=1)
        ttk.Label(waveform_bar, textvariable=self.__preview_text).grid(row=0, column=0, sticky="w")
        self.__previous_button = ttk.Button(waveform_bar, text="Previous", command=self.__show_previous)
        self.__previous_button.grid(row=0, column=1, padx=(8, 3))
        ttk.Label(waveform_bar, textvariable=self.__history_text, width=8, anchor=tk.CENTER).grid(row=0, column=2)
        self.__next_button = ttk.Button(waveform_bar, text="Next", command=self.__show_next)
        self.__next_button.grid(row=0, column=3, padx=(3, 8))
        ttk.Button(waveform_bar, text="Clear", command=self.__clear_scope).grid(row=0, column=4)

        scope_host = ttk.Frame(waveform)
        scope_host.grid(row=1, column=0, sticky="nsew")
        self.__scope = PhosphorScopeTk(
            scope_host,
            tlim=(-1e-6, 3e-6),
            vlim=(-0.5, 1.0),
            grid_shape=(240, 720),
            decay=0.97,
            gain=0.8,
            update_ms=30,
        )

        controls = ttk.Frame(outer)
        controls.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        controls.columnconfigure(0, weight=1)
        controls.columnconfigure(1, weight=1)

        diagnostic = ttk.LabelFrame(controls, text="Diagnostic Capture", padding=8)
        diagnostic.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        for column in range(4):
            diagnostic.columnconfigure(column, weight=1)
        self.__start_button = ttk.Button(diagnostic, text="Start Continuous", command=self.__start_continuous)
        self.__one_shot_button = ttk.Button(diagnostic, text="One Shot", command=self.__one_shot)
        self.__flush_button = ttk.Button(diagnostic, text="Flush", command=self.__flush)
        self.__stop_button = ttk.Button(diagnostic, text="Stop", command=self.__stop)
        self.__start_button.grid(row=0, column=0, sticky="ew", padx=(0, 3))
        self.__one_shot_button.grid(row=0, column=1, sticky="ew", padx=3)
        self.__flush_button.grid(row=0, column=2, sticky="ew", padx=3)
        self.__stop_button.grid(row=0, column=3, sticky="ew", padx=(3, 0))

        simulator = ttk.LabelFrame(controls, text="Simulator Inputs", padding=8)
        simulator.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
        self.__laser_toggle = ttk.Checkbutton(
            simulator,
            text="Laser enabled",
            variable=self.__laser_enabled,
            command=lambda: self.__set_simulator("laser_enabled", self.__laser_enabled.get()),
        )
        self.__chopper_toggle = ttk.Checkbutton(
            simulator,
            text="Chopper enabled",
            variable=self.__chopper_enabled,
            command=lambda: self.__set_simulator("chopper_enabled", self.__chopper_enabled.get()),
        )
        self.__pll_toggle = ttk.Checkbutton(
            simulator,
            text="PLL locked",
            variable=self.__pll_locked,
            command=lambda: self.__set_simulator("pll_locked", self.__pll_locked.get()),
        )
        self.__restore_button = ttk.Button(simulator, text="Restore Nominal", command=self.__restore_simulator)
        self.__laser_toggle.grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.__chopper_toggle.grid(row=0, column=1, sticky="w", padx=(0, 10))
        self.__pll_toggle.grid(row=0, column=2, sticky="w", padx=(0, 10))
        self.__restore_button.grid(row=0, column=3, sticky="e")
        simulator.columnconfigure(3, weight=1)

        recovery = ttk.LabelFrame(controls, text="Recovery", padding=8)
        recovery.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.__authorize_recovery_button = ttk.Button(
            recovery,
            text="Authorize Pulse Recovery",
            command=self.__authorize_recovery,
        )
        self.__recover_orphan_button = ttk.Button(
            recovery,
            text="Recover Orphaned Capture",
            command=self.__recover_orphan,
        )
        self.__authorize_recovery_button.pack(side=tk.LEFT, padx=(0, 6))
        self.__recover_orphan_button.pack(side=tk.LEFT)

        ttk.Label(outer, textvariable=self.__result_text, anchor=tk.W).grid(
            row=3, column=0, sticky="ew", pady=(6, 0)
        )
        self.__render_controls()
        self.__render_history_controls()

    def __setup_client(self, dds_ip: str) -> None:
        client_uuid = uuid.uuid4()
        self.__client = client.DDSClient(client_uuid, ip=dds_ip)

        def on_ready() -> None:
            if not self.__run or self.__dds_connected:
                return
            try:
                handle = self.__client.register_subsystem(
                    f"__acquisition_gui_{client_uuid}",
                    uuid.uuid4(),
                    temporary=True,
                )
                self.__subsystem = handle
                self.__status_kv = handle.add_remote_kv(
                    uuids.UUID_EUV_ACQUISITION_CONTROLLER,
                    dds_subsystem.KVDescriptor(types.ByteTypeSpecifier(), b"acquisition_status", True, True, False),
                )
                self.__preview_kv = handle.add_remote_kv(
                    uuids.UUID_EUV_ACQUISITION_CONTROLLER,
                    dds_subsystem.KVDescriptor(types.ByteTypeSpecifier(), b"acquisition_preview", True, True, False),
                )
                self.__cadence_kv = handle.add_remote_kv(
                    uuids.UUID_EUV_ACQUISITION_CONTROLLER,
                    dds_subsystem.KVDescriptor(types.ByteTypeSpecifier(), b"acquisition_cadence", True, True, False),
                )
                self.__status_kv.on_new_data_received(self.__on_status)
                self.__preview_kv.on_new_data_received(self.__on_preview)
                self.__cadence_kv.on_new_data_received(self.__on_cadence)
                for event_name in (
                    *DIAGNOSTIC_EVENTS.values(),
                    SIMULATOR_SET_EVENT,
                    SIMULATOR_RESTORE_EVENT,
                    AUTHORIZE_RECOVERY_EVENT,
                    RECOVER_ORPHAN_EVENT,
                ):
                    self.__events[event_name] = handle.add_event_provider(event_name)
            except Exception as exc:
                self.__ui_queue.put(("connection_error", str(exc)))
                return
            self.__dds_connected = True
            self.__ui_queue.put(("connected", None))

        self.__client.when_ready().then(on_ready)

    def __on_status(self, payload) -> None:
        try:
            status = decode_acquisition_status(payload)
        except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return
        self.__ui_queue.put(("status", status))

    def __on_preview(self, payload) -> None:
        try:
            raw = bytes(payload) if isinstance(payload, list) else bytes(payload)
        except (TypeError, ValueError):
            return
        self.__work_queue.put(("preview", raw))

    def __on_cadence(self, payload) -> None:
        try:
            raw = bytes(payload) if isinstance(payload, list) else bytes(payload)
        except (TypeError, ValueError):
            return
        self.__work_queue.put(("cadence", raw))

    def __worker_loop(self) -> None:
        pending: list[_PendingEvent] = []
        while not self.__stop_event.is_set():
            try:
                work = self.__work_queue.get(timeout=0.05)
            except queue.Empty:
                work = None
            if work is not None:
                kind, payload = work
                if kind == "preview":
                    try:
                        preview = AcquisitionPreview.decode(payload)
                    except Exception as exc:
                        self.__ui_queue.put(("result", ("preview", f"Rejected waveform preview: {exc}")))
                    else:
                        self.__ui_queue.put(("preview", preview))
                elif kind == "cadence":
                    try:
                        cadence = decode_live_cadence(payload)
                    except Exception as exc:
                        self.__ui_queue.put(("result", ("cadence", f"Rejected cadence data: {exc}")))
                    else:
                        self.__ui_queue.put(("cadence", cadence))
                elif kind == "cadence_plot":
                    run_id = payload
                    reader = None
                    try:
                        if self.__data_path is None:
                            raise RuntimeError("Experiment data path is unavailable.")
                        reader = ExperimentReader(self.__data_path, "exposure")
                        run = reader.locate_run_by_uuid(run_id)
                        if run is None:
                            raise FileNotFoundError(f"Exposure {run_id} was not found.")
                        result = ensure_capture_cadence_graph(run_id, run.get_record(), self.__data_path)
                        if result.graph is None:
                            raise RuntimeError(f"Capture cadence is {result.status.replace('_', ' ')}.")
                    except Exception as exc:
                        self.__ui_queue.put(("cadence_plot_error", str(exc)))
                    else:
                        self.__ui_queue.put(("cadence_plot_ready", result.graph))
                    finally:
                        if reader is not None:
                            reader.close()
                elif kind == "event":
                    action, event_name, event_payload, label, timeout_seconds = payload
                    provider = self.__events.get(event_name)
                    if provider is None:
                        self.__ui_queue.put(("result", (action, f"{label} failed: DDS event is unavailable.")))
                    else:
                        try:
                            handle = provider.call(event_payload, [uuids.UUID_EUV_ACQUISITION_CONTROLLER])
                        except Exception as exc:
                            self.__ui_queue.put(("result", (action, f"{label} failed: {exc}")))
                        else:
                            if handle is None:
                                self.__ui_queue.put(("result", (action, f"{label} failed: request could not be sent.")))
                            else:
                                pending.append(
                                    _PendingEvent(action, label, handle, time.monotonic() + timeout_seconds)
                                )
            self.__poll_pending_events(pending)

    def __poll_pending_events(self, pending: list[_PendingEvent]) -> None:
        now = time.monotonic()
        for operation in tuple(pending):
            if operation.handle.is_in_progress() and now < operation.deadline:
                state = operation.handle.get_state(uuids.UUID_EUV_ACQUISITION_CONTROLLER)
                if state == client.EVENT_IN_PROGRESS:
                    feedback = self.__decode_result(operation.handle.get_result(uuids.UUID_EUV_ACQUISITION_CONTROLLER))
                    if feedback and feedback != operation.last_feedback:
                        operation.last_feedback = feedback
                        self.__ui_queue.put(("feedback", (operation.action, feedback)))
                continue
            pending.remove(operation)
            if operation.handle.is_in_progress():
                message = f"{operation.label} failed: timed out."
            else:
                state = operation.handle.get_state(uuids.UUID_EUV_ACQUISITION_CONTROLLER)
                result = self.__decode_result(operation.handle.get_result(uuids.UUID_EUV_ACQUISITION_CONTROLLER))
                if state == client.EVENT_OK:
                    message = result or f"{operation.label} complete."
                else:
                    message = f"{operation.label} failed: {result or 'unknown error.'}"
            self.__ui_queue.put(("result", (operation.action, message)))

    @staticmethod
    def __decode_result(value) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            if value.startswith(magics.OP_OK):
                value = value[len(magics.OP_OK):]
            elif value.startswith(magics.OP_IN_PROGRESS):
                value = value[len(magics.OP_IN_PROGRESS):]
            return value.decode("utf-8", errors="replace").strip()
        return str(value).strip()

    def __queue_event(
        self,
        action: str,
        event_name: bytes,
        payload: bytes,
        label: str,
        timeout_seconds: float,
    ) -> None:
        if action in self.__pending_actions:
            return
        self.__pending_actions.add(action)
        self.__result_text.set(f"{label} requested.")
        self.__render_controls()
        self.__work_queue.put(("event", (action, event_name, payload, label, timeout_seconds)))

    def __start_continuous(self) -> None:
        self.__queue_event("start", DIAGNOSTIC_EVENTS["start"], bytes(), "Continuous diagnostic", 20.0)

    def __one_shot(self) -> None:
        self.__queue_event("one_shot", DIAGNOSTIC_EVENTS["one_shot"], bytes(), "One-shot diagnostic", 30.0)

    def __flush(self) -> None:
        self.__queue_event("flush", DIAGNOSTIC_EVENTS["flush"], bytes(), "Diagnostic flush", 20.0)

    def __stop(self) -> None:
        self.__queue_event("stop", DIAGNOSTIC_EVENTS["stop"], bytes(), "Diagnostic stop", 30.0)

    def __set_simulator(self, name: str, enabled: bool) -> None:
        payload = json.dumps({"name": name, "enabled": enabled}, separators=(",", ":")).encode("utf-8")
        self.__queue_event(f"simulator:{name}", SIMULATOR_SET_EVENT, payload, f"Set {name}", 15.0)

    def __restore_simulator(self) -> None:
        self.__queue_event("simulator:restore", SIMULATOR_RESTORE_EVENT, bytes(), "Restore simulator inputs", 15.0)

    def __authorize_recovery(self) -> None:
        self.__queue_event(
            "authorize_recovery",
            AUTHORIZE_RECOVERY_EVENT,
            bytes(),
            "Pulse recovery authorization",
            15.0,
        )

    def __recover_orphan(self) -> None:
        if not messagebox.askyesno(
            "Recover Orphaned Capture",
            "Import all unacknowledged artifacts, reconcile the matching exposure, and release the digitizer spool?",
            parent=self.__root,
        ):
            return
        self.__queue_event(
            "recover_orphan",
            RECOVER_ORPHAN_EVENT,
            b"confirm",
            "Orphaned capture recovery",
            120.0,
        )

    def __ui_tick(self) -> None:
        while True:
            try:
                kind, payload = self.__ui_queue.get_nowait()
            except queue.Empty:
                break
            if kind == "connected":
                self.__connection_text.set("DDS: connected")
            elif kind == "connection_error":
                self.__connection_text.set("DDS: unavailable")
                self.__result_text.set(f"DDS setup failed: {payload}")
            elif kind == "status":
                self.__status = payload
                self.__render_status(payload)
            elif kind == "preview":
                self.__display_new_preview(payload)
            elif kind == "cadence":
                self.__cadence = payload
                self.__cadence_chart.update(payload)
                self.__render_cadence_metrics()
            elif kind == "cadence_plot_ready":
                self.__cadence_plot_pending = False
                try:
                    show_capture_cadence_figure(payload)
                except Exception as exc:
                    self.__result_text.set(f"Could not open capture integrity graph: {exc}")
                else:
                    self.__result_text.set("Opened capture integrity graph for the last exposure.")
            elif kind == "cadence_plot_error":
                self.__cadence_plot_pending = False
                self.__result_text.set(f"Could not prepare capture integrity graph: {payload}")
            elif kind == "feedback":
                _action, message = payload
                self.__result_text.set(message)
            elif kind == "result":
                action, message = payload
                self.__pending_actions.discard(action)
                self.__result_text.set(message)
            self.__render_controls()
        if self.__run:
            self.__ui_job = self.__root.after(self.UI_UPDATE_MS, self.__ui_tick)

    def __select_cadence_window(self) -> None:
        self.__cadence_chart.set_window(float(self.__cadence_window.get()))
        self.__render_cadence_metrics()

    def __open_last_cadence(self) -> None:
        if self.__cadence_plot_pending or not isinstance(self.__status, dict):
            return
        value = self.__status.get("last_run_id")
        try:
            run_id = uuid.UUID(str(value))
        except (TypeError, ValueError, AttributeError):
            self.__result_text.set("No completed exposure is available for capture integrity analysis.")
            return
        self.__cadence_plot_pending = True
        self.__result_text.set("Preparing capture integrity graph for the last exposure...")
        self.__render_controls()
        self.__work_queue.put(("cadence_plot", run_id))

    def __render_cadence_metrics(self) -> None:
        cadence = self.__cadence
        if cadence is None:
            return
        window_seconds = float(self.__cadence_window.get())
        latest_window = next(
            (
                window
                for point in reversed(cadence.points)
                for window in point.windows
                if window.window_seconds == window_seconds and window.capture_rate_hz is not None
            ),
            None,
        )
        capture_rate = None if latest_window is None else latest_window.capture_rate_hz
        lost_rate = None if latest_window is None else latest_window.estimated_lost_per_second
        self.__cadence_rate_text.set(
            "Capture rate: N/A" if capture_rate is None else f"Capture rate: {capture_rate:.1f} Hz"
        )
        self.__cadence_loss_text.set(
            "Estimated lost: N/A" if lost_rate is None else f"Estimated lost: {lost_rate:.2f}/s"
        )
        omitted = (
            ""
            if cadence.omitted_gap_count == 0
            else f" ({cadence.omitted_gap_count} live marker(s) omitted)"
        )
        self.__cadence_total_text.set(f"Estimated missing: {cadence.inferred_lost_count}{omitted}")
        self.__cadence_quality_text.set(f"Evidence: {cadence.quality.value.replace('_', '-')}")

    def __render_status(self, status: dict) -> None:
        state = str(status.get("state", "unknown"))
        self.__state_text.set(f"Acquisition: {state.replace('_', ' ')}")
        source_kind = status.get("source_kind") or "unknown"
        source_id = status.get("source_id")
        self.__source_text.set(f"Source: {source_kind}" + (f" / {source_id}" if source_id else ""))
        self.__detail_text.set(acquisition_status_detail(status))
        dose, runtime = acquisition_status_metrics(status)
        self.__dose_text.set(f"Dose: {dose}")
        self.__runtime_text.set(f"Transmitting time: {runtime}")
        pipeline = acquisition_pipeline_status(status)
        self.__pipeline_mode_text.set(f"Mode: {pipeline.mode}")
        self.__pipeline_rate_text.set(f"Accepted: {pipeline.accepted_rate}")
        self.__pipeline_queues_text.set(f"Queues: {pipeline.queues}")
        self.__pipeline_timings_text.set(f"p95: {pipeline.timings}")
        self.__pipeline_fallback_text.set(f"Fallback: {pipeline.fallback}")
        self.__pipeline_fault_text.set(f"Fault: {pipeline.fault}")

        simulator = status.get("simulator")
        if isinstance(simulator, dict):
            for variable, name in (
                (self.__laser_enabled, "laser_enabled"),
                (self.__chopper_enabled, "chopper_enabled"),
                (self.__pll_locked, "pll_locked"),
            ):
                value = simulator.get(name)
                if isinstance(value, bool):
                    variable.set(value)

    def __render_controls(self) -> None:
        allowed = acquisition_control_state(self.__status, dds_connected=self.__dds_connected)
        diagnostic_pending = bool(self.__pending_actions & {"start", "one_shot", "flush", "stop"})
        self.__set_widget_enabled(self.__start_button, allowed.start_enabled and not diagnostic_pending)
        self.__set_widget_enabled(self.__one_shot_button, allowed.one_shot_enabled and not diagnostic_pending)
        self.__set_widget_enabled(self.__flush_button, allowed.flush_enabled and not diagnostic_pending)
        self.__set_widget_enabled(self.__stop_button, allowed.stop_enabled and not diagnostic_pending)
        simulator_pending = any(action.startswith("simulator:") for action in self.__pending_actions)
        for widget in (self.__laser_toggle, self.__chopper_toggle, self.__pll_toggle, self.__restore_button):
            self.__set_widget_enabled(widget, allowed.simulator_enabled and not simulator_pending)
        self.__set_widget_enabled(
            self.__authorize_recovery_button,
            allowed.authorize_recovery_enabled and "authorize_recovery" not in self.__pending_actions,
        )
        self.__set_widget_enabled(
            self.__recover_orphan_button,
            allowed.recover_orphan_enabled and "recover_orphan" not in self.__pending_actions,
        )
        last_run_id = self.__status.get("last_run_id") if isinstance(self.__status, dict) else None
        try:
            uuid.UUID(str(last_run_id))
            has_last_run = True
        except (TypeError, ValueError, AttributeError):
            has_last_run = False
        self.__set_widget_enabled(
            self.__open_cadence_button,
            self.__data_path is not None and has_last_run and not self.__cadence_plot_pending,
        )

    @staticmethod
    def __set_widget_enabled(widget, enabled: bool) -> None:
        widget.state(["!disabled"] if enabled else ["disabled"])

    def __display_new_preview(self, preview: AcquisitionPreview) -> None:
        previous_session = self.__history.session_id
        self.__history_index = self.__history.append(preview)
        if previous_session != preview.session_id:
            self.__scope.clear()
        self.__display_preview(preview)

    def __display_preview(self, preview: AcquisitionPreview) -> None:
        self.__scope.set_time_limits((-preview.pretrigger_seconds, preview.window_seconds - preview.pretrigger_seconds))
        self.__scope.push(preview.to_pulses())
        self.__preview_text.set(
            f"{preview.context.title()} | {preview.included_pulse_count}/{preview.total_pulse_count} traces | "
            f"sequence {int(preview.sequence[0])}-{int(preview.sequence[-1])}"
        )
        self.__render_history_controls()

    def __show_previous(self) -> None:
        if self.__history_index <= 0:
            return
        self.__history_index -= 1
        self.__scope.clear()
        self.__display_preview(self.__history[self.__history_index])

    def __show_next(self) -> None:
        if self.__history_index < 0 or self.__history_index >= len(self.__history) - 1:
            return
        self.__history_index += 1
        self.__scope.clear()
        self.__display_preview(self.__history[self.__history_index])

    def __render_history_controls(self) -> None:
        count = len(self.__history)
        position = self.__history_index + 1 if count else 0
        self.__history_text.set(f"{position} / {count}")
        self.__set_widget_enabled(self.__previous_button, self.__history_index > 0)
        self.__set_widget_enabled(self.__next_button, 0 <= self.__history_index < count - 1)

    def __clear_scope(self) -> None:
        self.__scope.clear()

    def ok(self) -> bool:
        return self.__run and self.__client is not None and self.__client.ok() and self.__worker.is_alive()

    def close(self) -> None:
        if not self.__run:
            return
        self.__run = False
        if self.__ui_job is not None:
            try:
                self.__root.after_cancel(self.__ui_job)
            except tk.TclError:
                pass
            self.__ui_job = None
        self.__scope.close()
        self.__cadence_chart.close()
        self.__stop_event.set()
        self.__worker.join(timeout=2.0)
        if self.__client is not None:
            self.__client.close()


def main() -> None:
    root = tk.Tk()
    application = AcquisitionGUI(root)
    try:
        root.mainloop()
    finally:
        application.close()


if __name__ == "__main__":
    main()