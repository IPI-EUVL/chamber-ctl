from __future__ import annotations

import json
import math
import queue
import threading
import time
import uuid
import tkinter as tk
from collections.abc import Callable, Iterable
from dataclasses import replace
from tkinter import messagebox, ttk

from ipi_ecs.dds import client, subsystem, types

from chamber_ctl import ECS_IP
from chamber_ctl.data.calibration import CalibrationRepository, SourceKey
from chamber_ctl.gui.sample_motion_gui import RING_RADII, build_sample_data
from chamber_ctl.gui.source_calibration_editor import (
    calibration_option,
    edit_source_calibrations,
    source_calibration_summary,
)
from chamber_ctl.subsystems import uuids
from chamber_ctl.subsystems.batch_controller import decode_batch_state, encode_batch_command
from chamber_ctl.subsystems.batcher import (
    BatchPlan,
    BatchPlanEntry,
    ControlGenerator,
    ExecutionMode,
    ExposureTemplate,
    LinearContrastDoseGenerator,
    TargetAssignmentGenerator,
    TargetMode,
    apply_plan_generator,
    batch_plan_from_dict,
    batch_plan_to_dict,
)
from chamber_ctl.subsystems.settings_presets import SettingsPresets


class BatchControllerClient:
    def __init__(self, host: str = ECS_IP) -> None:
        self._commands: queue.Queue[tuple[str, dict]] = queue.Queue()
        self._messages: queue.Queue[tuple[str, object]] = queue.Queue()
        self._stop = threading.Event()
        self._state_kv = None
        self._command_event = None
        self._configured = False
        client_uuid = uuid.uuid4()
        self._client = client.DDSClient(client_uuid, ip=host)

        def on_ready():
            if self._configured:
                return
            self._configured = True
            subsystem_uuid = uuid.uuid4()
            handle = self._client.register_subsystem(
                f"__batch_gui_{subsystem_uuid}",
                subsystem_uuid,
                temporary=True,
            )
            self._state_kv = handle.add_remote_kv(
                uuids.UUID_EXPOSURE_BATCH_CONTROLLER,
                subsystem.KVDescriptor(types.ByteTypeSpecifier(), b"state", True, True, False),
            )
            self._state_kv.on_new_data_received(self._on_state)
            self._command_event = handle.add_event_provider(b"batch_command")
            self._messages.put(("connected", None))
            self.request("refresh")

        self._client.when_ready().then(on_ready)
        self._thread = threading.Thread(target=self._worker, name="batch-gui-dds", daemon=True)
        self._thread.start()

    def _on_state(self, payload) -> None:
        try:
            state = decode_batch_state(bytes(payload))
        except Exception as exc:
            self._messages.put(("error", f"State decode failed: {exc}"))
        else:
            self._messages.put(("state", state))

    def request(self, command: str, args: dict | None = None) -> None:
        self._commands.put((command, args or {}))

    def pop_messages(self) -> list[tuple[str, object]]:
        messages = []
        while True:
            try:
                messages.append(self._messages.get_nowait())
            except queue.Empty:
                return messages

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                command, args = self._commands.get(timeout=0.2)
            except queue.Empty:
                continue
            event = self._command_event
            if event is None:
                self._messages.put(("error", "Batch Controller is not connected."))
                continue
            handle = event.call(
                encode_batch_command(command, args),
                [uuids.UUID_EXPOSURE_BATCH_CONTROLLER],
            )
            if handle is None:
                self._messages.put(("error", f"Could not send {command}."))
                continue
            started = time.monotonic()
            while handle.is_in_progress() and time.monotonic() - started < 60.0 and not self._stop.is_set():
                time.sleep(0.1)
            if handle.is_in_progress():
                self._messages.put(("error", f"Batch command {command} timed out."))
                continue
            result = handle.get_result(uuids.UUID_EXPOSURE_BATCH_CONTROLLER)
            state = handle.get_state(uuids.UUID_EXPOSURE_BATCH_CONTROLLER)
            try:
                response = json.loads(bytes(result).decode("utf-8")) if result else {}
            except Exception:
                response = {"ok": False, "error": bytes(result).decode("utf-8", errors="replace") if result else "No response"}
            if state != client.EVENT_OK or not response.get("ok"):
                self._messages.put(("error", response.get("error") or f"Batch command {command} failed."))
            else:
                self._messages.put(("result", (command, response.get("result"))))

    def close(self) -> None:
        self._stop.set()
        self._client.close()
        self._thread.join(timeout=2.0)


class BatchControllerGUI:
    def __init__(
        self,
        root,
        data_path: str,
        own_window: bool = True,
        source_options_provider: Callable[[], Iterable[SourceKey]] | None = None,
    ) -> None:
        self._root = root
        self._own_window = own_window
        self._client = BatchControllerClient()
        self._presets = SettingsPresets(data_path)
        self._data_path = data_path
        self._source_options_provider = source_options_provider
        self._source_calibration_options: dict[str, tuple[str, int]] = {}
        self._state = None
        self._manifest_uuid: str | None = None
        self._manifest_revision: int | None = None
        self._manifests_by_uuid: dict[str, dict] = {}
        self._entries: list[BatchPlanEntry] = []
        self._ordered_samples: list[int] = []
        self._template_value = ExposureTemplate("")
        self._detail_widgets = {}
        self._detail_values_loading = False
        self._override_dirty_fields: set[str] = set()
        self._override_conflict_fields: set[str] = set()
        self._status = tk.StringVar(value="Connecting to Batch Controller...")
        self._status_phase = tk.StringVar(value="CONNECTING")
        self._status_batch = tk.StringVar(value="No active batch")
        self._status_action = tk.StringVar(value="Waiting for Batch Controller state.")
        self._status_next = tk.StringVar(value="")
        self._status_remaining = tk.StringVar(value="Remaining: —")
        self._mode = tk.StringVar(value=ExecutionMode.MANUAL.value)
        self._generator = tk.StringVar(value="Target assignment")
        self._target_mode = tk.StringVar(value=TargetMode.DOSE.value)
        self._target_value = tk.StringVar(value="10")
        self._contrast_min_value = tk.StringVar(value="10")
        self._contrast_max_value = tk.StringVar(value="100")
        self._manifest_filter = tk.StringVar(value="All batches")
        self._details_mode = tk.StringVar(value="Batch default exposure details")
        self._details_hint = tk.StringVar(value="Select planned samples on the stage to edit their overrides.")
        self._source_calibration_text = tk.StringVar(value="None")
        self._samples = build_sample_data()
        self._styles = ttk.Style()
        self._styles.configure("BatchStatus.Good.TLabel", foreground="#217a3b")
        self._styles.configure("BatchStatus.Warning.TLabel", foreground="#a66000")
        self._styles.configure("BatchStatus.Error.TLabel", foreground="#b3261e")
        self._styles.configure("BatchStatus.Active.TLabel", foreground="#1261a0")
        self._styles.configure("BatchStatus.Neutral.TLabel", foreground="#4d5d67")
        self._generator.trace_add("write", lambda *_args: self._render_generator_parameters())
        self._build()
        self._capture_template_from_form()
        self._update_control_states()
        self._load_presets()
        self._root.after(200, self._update)

    def _load_presets(self) -> None:
        self._operator["values"] = self._presets.read_operators()
        self._zr_filter["values"] = self._presets.read_zr_filters()
        self._sample_type["values"] = self._presets.read_sample_types()
        repository = CalibrationRepository(self._data_path)
        try:
            source_profiles = repository.list_all()
        finally:
            repository.close()
        self._source_calibration_options = {
            f"{profile.name} r{profile.revision} | {profile.profile_id}": (str(profile.profile_id), profile.revision)
            for profile in source_profiles
        }

    def _build(self) -> None:
        if self._own_window and hasattr(self._root, "title"):
            self._root.title("Exposure Batch Controller")
        outer = ttk.Frame(self._root, padding=8)
        outer.pack(fill=tk.BOTH, expand=True)
        outer.columnconfigure(0, minsize=340)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(0, weight=1)

        status = ttk.LabelFrame(outer, text="Live batch status", padding=10)
        status.grid(row=0, column=0, sticky=tk.NSEW, padx=(0, 6))
        status.columnconfigure(0, weight=1)
        status.rowconfigure(6, weight=1)
        self._status_phase_label = ttk.Label(
            status,
            textvariable=self._status_phase,
            font=("TkDefaultFont", 22, "bold"),
            style="BatchStatus.Neutral.TLabel",
        )
        self._status_phase_label.grid(
            row=0, column=0, sticky=tk.W
        )
        ttk.Label(status, textvariable=self._status_batch, font=("TkDefaultFont", 11, "bold"), wraplength=310).grid(
            row=1, column=0, sticky=tk.W, pady=(8, 0)
        )
        self._status_action_label = ttk.Label(
            status,
            textvariable=self._status_action,
            justify=tk.LEFT,
            wraplength=310,
        )
        self._status_action_label.grid(
            row=2, column=0, sticky=tk.W, pady=(8, 0)
        )
        ttk.Label(status, textvariable=self._status_next, justify=tk.LEFT, wraplength=310, foreground="#9a4d35").grid(
            row=3, column=0, sticky=tk.W, pady=(4, 0)
        )
        ttk.Label(status, textvariable=self._status_remaining, font=("TkDefaultFont", 10, "bold")).grid(
            row=4, column=0, sticky=tk.W, pady=(8, 0)
        )
        ttk.Label(status, text="Sample progress").grid(row=5, column=0, sticky=tk.W, pady=(10, 2))
        self._status_progress = ttk.Treeview(
            status,
            columns=("sample", "target", "actual", "remaining", "state"),
            show="headings",
            height=10,
        )
        for column, label, width in (
            ("sample", "Sample", 58),
            ("target", "Target", 65),
            ("actual", "Actual", 65),
            ("remaining", "Left", 58),
            ("state", "State", 88),
        ):
            self._status_progress.heading(column, text=label)
            self._status_progress.column(column, width=width, anchor=tk.W)
        self._status_progress.tag_configure("progress-under-target", foreground="#a66000")
        self._status_progress.tag_configure("progress-within-tolerance", foreground="#217a3b")
        self._status_progress.tag_configure("progress-overshot", foreground="#a66000")
        self._status_progress.grid(row=6, column=0, sticky=tk.NSEW)

        status_controls = ttk.LabelFrame(status, text="Live batch controls", padding=6)
        status_controls.grid(row=7, column=0, sticky=tk.EW, pady=(8, 0))
        for column in range(3):
            status_controls.columnconfigure(column, weight=1)
        self._continue_button = ttk.Button(status_controls, text="Continue", command=lambda: self._command("continue"))
        self._pause_button = ttk.Button(status_controls, text="Pause", command=lambda: self._command("pause"))
        self._resume_button = ttk.Button(status_controls, text="Acknowledge", command=lambda: self._command("resume"))
        self._cancel_button = ttk.Button(status_controls, text="Cancel", command=lambda: self._command("cancel"))
        self._refresh_button = ttk.Button(status_controls, text="Refresh", command=lambda: self._command("refresh"))
        for index, button in enumerate((
            self._continue_button,
            self._pause_button,
            self._resume_button,
            self._cancel_button,
            self._refresh_button,
        )):
            button.grid(row=index // 3, column=index % 3, sticky=tk.EW, padx=2, pady=2)
        mode_frame = ttk.Frame(status_controls)
        mode_frame.grid(row=2, column=0, columnspan=3, sticky=tk.W, pady=(6, 0))
        ttk.Label(mode_frame, text="Execution mode").pack(side=tk.LEFT)
        self._manual_mode_button = ttk.Radiobutton(
            mode_frame,
            text="Manual",
            variable=self._mode,
            value=ExecutionMode.MANUAL.value,
            command=self._set_execution_mode,
        )
        self._manual_mode_button.pack(side=tk.LEFT, padx=(8, 0))
        self._automatic_mode_button = ttk.Radiobutton(
            mode_frame,
            text="Automatic",
            variable=self._mode,
            value=ExecutionMode.AUTOMATIC.value,
            command=self._set_execution_mode,
        )
        self._automatic_mode_button.pack(side=tk.LEFT, padx=(8, 0))

        editor = ttk.Frame(outer)
        editor.grid(row=0, column=1, sticky=tk.NSEW)
        editor.columnconfigure(0, weight=1)
        editor.rowconfigure(1, weight=1)

        shared = ttk.LabelFrame(editor, text="Exposure details", padding=6)
        shared.grid(row=0, column=0, sticky=tk.EW)
        for index in range(8):
            shared.columnconfigure(index, weight=1 if index % 2 else 0)
        self._name = self._entry(shared, "Name", 0, 0)
        self._description = self._entry(shared, "Description", 0, 2)
        self._operator = self._combo(shared, "Operator", 0, 4)
        self._sample_type = self._combo(shared, "Sample type", 0, 6)
        self._zr_filter = self._combo(shared, "Zr filter", 1, 0)
        self._base_pressure = self._entry(shared, "Base pressure", 1, 2, "0")
        self._operating_pressure = self._entry(shared, "Operating pressure", 1, 4, "0")
        self._flow = self._entry(shared, "Flow SCCM", 1, 6, "0")
        self._chopper_frequency = self._entry(shared, "Chopper Hz", 2, 0, "192")
        ttk.Label(shared, text="Acquisition sources").grid(row=2, column=2, sticky=tk.W, padx=(0, 3))
        self._source_calibration_button = ttk.Button(
            shared,
            text="Configure...",
            command=self._edit_source_calibrations,
        )
        self._source_calibration_button.grid(row=2, column=3, sticky=tk.W, padx=(0, 8), pady=2)
        ttk.Label(
            shared,
            textvariable=self._source_calibration_text,
            wraplength=310,
        ).grid(row=2, column=4, columnspan=4, sticky=tk.W)
        self._detail_widgets = {
            "name": self._name,
            "description": self._description,
            "operator": self._operator,
            "sample_type": self._sample_type,
            "zr_filter": self._zr_filter,
            "base_pressure": self._base_pressure,
            "operating_pressure": self._operating_pressure,
            "flow_sccm": self._flow,
        }
        for key, widget in self._detail_widgets.items():
            widget.bind("<KeyRelease>", lambda _event, field=key: self._mark_override_dirty(field), add="+")
            if isinstance(widget, ttk.Combobox):
                widget.bind("<<ComboboxSelected>>", lambda _event, field=key: self._mark_override_dirty(field), add="+")
        ttk.Label(shared, textvariable=self._details_mode, font=("TkDefaultFont", 9, "bold")).grid(
            row=3, column=0, columnspan=8, sticky=tk.W, pady=(5, 0)
        )
        ttk.Label(shared, textvariable=self._details_hint, wraplength=760).grid(
            row=4, column=0, columnspan=8, sticky=tk.W, pady=(2, 0)
        )
        self._apply_overrides_button = ttk.Button(
            shared,
            text="Apply selected overrides",
            command=self._apply_selected_overrides,
            state=tk.DISABLED,
        )
        self._apply_overrides_button.grid(row=5, column=0, columnspan=2, sticky=tk.W, pady=(5, 0))
        self._clear_overrides_button = ttk.Button(
            shared,
            text="Clear selected overrides",
            command=self._clear_selected_overrides,
            state=tk.DISABLED,
        )
        self._clear_overrides_button.grid(row=5, column=2, columnspan=2, sticky=tk.W, pady=(5, 0))

        body = ttk.PanedWindow(editor, orient=tk.HORIZONTAL)
        body.grid(row=1, column=0, sticky=tk.NSEW, pady=(6, 0))
        stage = ttk.LabelFrame(body, text="Sample stage selection", padding=6)
        generator = ttk.LabelFrame(body, text="Plan generator", padding=6)
        plan_panel = ttk.LabelFrame(body, text="Plan entries", padding=6)
        body.add(stage, weight=3)
        body.add(generator, weight=1)
        body.add(plan_panel, weight=2)

        stage.columnconfigure(0, weight=1)
        stage.rowconfigure(1, weight=1)
        ttk.Label(stage, text="Click a sample to select or deselect it. Selection order controls contrast-dose assignment.").grid(
            row=0, column=0, sticky=tk.W
        )
        self._stage_canvas = tk.Canvas(stage, width=520, height=390, background="#f7fafc", highlightthickness=0)
        self._stage_canvas.grid(row=1, column=0, sticky=tk.NSEW, pady=(4, 0))
        self._stage_canvas.bind("<Configure>", lambda _event: self._render_stage())
        self._selection_order_text = tk.StringVar(value="Selection order: none")
        ttk.Label(stage, textvariable=self._selection_order_text, wraplength=520).grid(row=2, column=0, sticky=tk.W, pady=(6, 0))

        generator.columnconfigure(0, weight=1)
        ttk.Label(generator, text="Selected samples are taken from the stage in the displayed selection order.", wraplength=245).grid(
            row=0, column=0, sticky=tk.W
        )
        ttk.Label(generator, text="Generator").grid(row=1, column=0, sticky=tk.W, pady=(10, 0))
        ttk.Combobox(
            generator,
            textvariable=self._generator,
            values=("Target assignment", "Linear contrast dose", "Zero-dose control"),
            state="readonly",
            width=24,
        ).grid(row=2, column=0, sticky=tk.EW)
        self._generator_parameters = ttk.Frame(generator)
        self._generator_parameters.grid(row=3, column=0, sticky=tk.EW, pady=(8, 0))
        self._render_generator_parameters()
        ttk.Button(generator, text="Apply generator to selected samples", command=self._apply_generator).grid(row=4, column=0, sticky=tk.EW, pady=(8, 0))

        inventory = ttk.LabelFrame(generator, text="Saved batches", padding=6)
        inventory.grid(row=5, column=0, sticky=tk.NSEW, pady=(10, 0))
        for column in range(3):
            inventory.columnconfigure(column, weight=1)
        ttk.Label(inventory, text="Show").grid(row=0, column=0, sticky=tk.W)
        filter_box = ttk.Combobox(
            inventory,
            textvariable=self._manifest_filter,
            values=("All batches", "Draft / active"),
            state="readonly",
            width=16,
        )
        filter_box.grid(row=0, column=1, columnspan=2, sticky=tk.EW, pady=(0, 4))
        filter_box.bind("<<ComboboxSelected>>", lambda _event: self._render_manifest_inventory())
        self._manifest_tree = ttk.Treeview(
            inventory,
            columns=("status", "revision", "name"),
            show="headings",
            height=7,
            selectmode="browse",
        )
        for column, label, width in (
            ("status", "Status", 70),
            ("revision", "Rev", 38),
            ("name", "Name", 212),
        ):
            self._manifest_tree.heading(column, text=label)
            self._manifest_tree.column(column, width=width, anchor=tk.W)
        self._manifest_tree.grid(row=1, column=0, columnspan=3, sticky=tk.EW)
        self._manifest_tree.bind("<<TreeviewSelect>>", lambda _event: self._update_control_states())
        self._load_button = ttk.Button(inventory, text="Load", command=self._load_selected)
        self._new_button = ttk.Button(inventory, text="New draft", command=self._new_draft)
        self._save_new_button = ttk.Button(inventory, text="Save new", command=self._save_new)
        self._save_revision_button = ttk.Button(inventory, text="Save revision", command=self._save_revision)
        self._activate_button = ttk.Button(inventory, text="Activate", command=self._activate)
        self._withdraw_button = ttk.Button(inventory, text="Withdraw", command=self._withdraw)
        self._restore_button = ttk.Button(inventory, text="Restore draft", command=self._unwithdraw)
        for index, button in enumerate((
            self._load_button,
            self._new_button,
            self._save_new_button,
            self._save_revision_button,
            self._activate_button,
            self._withdraw_button,
            self._restore_button,
        )):
            button.grid(row=2 + index // 3, column=index % 3, sticky=tk.EW, padx=2, pady=(6 if index < 3 else 2, 0))

        plan_panel.rowconfigure(0, weight=1)
        plan_panel.columnconfigure(0, weight=1)
        self._plan_tree = ttk.Treeview(
            plan_panel,
            columns=("order", "sample", "mode", "target"),
            show="headings",
            selectmode="browse",
        )
        for column, label, width in (
            ("order", "Order", 55),
            ("sample", "Sample", 70),
            ("mode", "Mode", 90),
            ("target", "Target", 110),
        ):
            self._plan_tree.heading(column, text=label)
            self._plan_tree.column(column, width=width, anchor=tk.W)
        self._plan_tree.grid(row=0, column=0, columnspan=4, sticky=tk.NSEW)
        ttk.Button(plan_panel, text="Remove", command=self._remove_entry).grid(row=1, column=0, sticky=tk.EW, pady=(6, 0))
        ttk.Button(plan_panel, text="Move up", command=lambda: self._move_entry(-1)).grid(row=1, column=1, sticky=tk.EW, pady=(6, 0))
        ttk.Button(plan_panel, text="Move down", command=lambda: self._move_entry(1)).grid(row=1, column=2, sticky=tk.EW, pady=(6, 0))

        ttk.Label(editor, textvariable=self._status, anchor=tk.W, relief=tk.SUNKEN).grid(row=2, column=0, sticky=tk.EW, pady=(6, 0))

    @staticmethod
    def _entry(parent, label, row, column, default="", columnspan=1):
        ttk.Label(parent, text=label).grid(row=row, column=column, sticky=tk.W, padx=(0, 3))
        entry = ttk.Entry(parent)
        entry.grid(row=row, column=column + 1, columnspan=columnspan, sticky=tk.EW, padx=(0, 8), pady=2)
        entry.insert(0, default)
        return entry

    @staticmethod
    def _combo(parent, label, row, column):
        ttk.Label(parent, text=label).grid(row=row, column=column, sticky=tk.W, padx=(0, 3))
        combo = ttk.Combobox(parent)
        combo.grid(row=row, column=column + 1, sticky=tk.EW, padx=(0, 8), pady=2)
        return combo

    def _template(self) -> ExposureTemplate:
        if not self._ordered_samples:
            self._capture_template_from_form()
        return self._template_value

    def _plan(self) -> BatchPlan:
        template = self._template()
        if template.primary_source is None:
            raise ValueError("Configure one acquisition source as Primary before saving the batch.")
        return BatchPlan(template, tuple(self._entries))

    def _capture_template_from_form(self) -> None:
        self._template_value = ExposureTemplate(
            name=self._name.get().strip(),
            description=self._description.get().strip(),
            operator=self._operator.get().strip(),
            zr_filter=self._zr_filter.get().strip(),
            sample_type=self._sample_type.get().strip(),
            base_pressure=float(self._base_pressure.get()),
            operating_pressure=float(self._operating_pressure.get()),
            flow_sccm=float(self._flow.get()),
            calibration_profile_id=self._template_value.calibration_profile_id,
            calibration_revision=self._template_value.calibration_revision,
            chopper_frequency_hz=float(self._chopper_frequency.get()),
            source_calibrations=self._template_value.source_calibrations,
            primary_source=self._template_value.primary_source,
        )

    def _edit_source_calibrations(self) -> None:
        selected = edit_source_calibrations(
            self._root,
            self._template_value.source_calibrations,
            self._source_calibration_options,
            self._template_value.primary_source,
            data_path=self._data_path,
            source_options=self._available_sources(),
            on_calibration_created=self._on_calibration_created,
        )
        if selected is None:
            return
        self._template_value = replace(
            self._template_value,
            source_calibrations=selected.calibrations,
            primary_source=selected.primary_source,
        )
        self._source_calibration_text.set(
            source_calibration_summary(selected.calibrations, selected.primary_source)
        )

    def _available_sources(self) -> tuple[SourceKey, ...]:
        if self._source_options_provider is None:
            return ()
        return tuple(sorted(set(self._source_options_provider())))

    def _on_calibration_created(self, profile) -> None:
        label, value = calibration_option(profile)
        self._source_calibration_options[label] = value

    def _set_detail_value(self, key: str, value) -> None:
        widget = self._detail_widgets[key]
        widget.delete(0, tk.END)
        widget.insert(0, "" if value is None else str(value))

    def _set_detail_widgets_enabled(self, enabled: bool) -> None:
        for widget in self._detail_widgets.values():
            widget.config(state=tk.NORMAL if enabled else tk.DISABLED)

    def _show_template_editor(self) -> None:
        self._detail_values_loading = True
        try:
            values = {
                "name": self._template_value.name,
                "description": self._template_value.description,
                "operator": self._template_value.operator,
                "zr_filter": self._template_value.zr_filter,
                "sample_type": self._template_value.sample_type,
                "base_pressure": self._template_value.base_pressure,
                "operating_pressure": self._template_value.operating_pressure,
                "flow_sccm": self._template_value.flow_sccm,
            }
            for key, value in values.items():
                self._set_detail_value(key, value)
            self._source_calibration_text.set(
                source_calibration_summary(
                    self._template_value.source_calibrations,
                    self._template_value.primary_source,
                )
            )
            self._set_detail_value("flow_sccm", self._template_value.flow_sccm)
            self._chopper_frequency.delete(0, tk.END)
            if self._template_value.chopper_frequency_hz is not None:
                self._chopper_frequency.insert(0, str(self._template_value.chopper_frequency_hz))
        finally:
            self._detail_values_loading = False
        self._override_dirty_fields.clear()
        self._override_conflict_fields.clear()
        self._details_mode.set("Batch default exposure details")
        self._details_hint.set("Select planned samples on the stage to edit their overrides.")
        self._set_detail_widgets_enabled(True)
        self._chopper_frequency.config(state=tk.NORMAL)
        self._apply_overrides_button.config(state=tk.DISABLED)
        self._clear_overrides_button.config(state=tk.DISABLED)

    def _planned_selected_entries(self) -> list[BatchPlanEntry]:
        by_sample = {entry.sample: entry for entry in self._entries}
        return [by_sample[sample] for sample in self._ordered_samples if sample in by_sample]

    def _show_override_editor(self) -> None:
        entries = self._planned_selected_entries()
        unplanned = [sample + 1 for sample in self._ordered_samples if sample not in {entry.sample for entry in entries}]
        self._detail_values_loading = True
        self._override_conflict_fields.clear()
        try:
            for key in self._detail_widgets:
                values = [entry.overrides.get(key) for entry in entries]
                value = values[0] if values and all(item == values[0] for item in values) else None
                if values and any(item != values[0] for item in values):
                    self._override_conflict_fields.add(key)
                self._set_detail_value(key, value)
        finally:
            self._detail_values_loading = False
        self._override_dirty_fields.clear()
        selection = ", ".join(f"Sample {sample + 1}" for sample in self._ordered_samples)
        self._details_mode.set(f"Overrides for {selection}")
        if not entries:
            self._details_hint.set("Assign plan targets to the selected samples before setting their overrides.")
        elif unplanned:
            self._details_hint.set(
                "All selected samples need plan targets before overrides can be applied: "
                + ", ".join(f"Sample {sample}" for sample in unplanned)
            )
        elif self._override_conflict_fields:
            fields = ", ".join(sorted(field.replace("_", " ") for field in self._override_conflict_fields))
            self._details_hint.set(
                f"Selected samples differ in {fields}. Type a value to apply it to all selected samples. Blank fields inherit defaults."
            )
        else:
            self._details_hint.set("Blank fields inherit the batch default. Changes apply when selection changes, on save, or with Apply selected overrides.")
        enabled = bool(entries) and not unplanned
        self._set_detail_widgets_enabled(enabled)
        self._chopper_frequency.config(state=tk.DISABLED)
        self._apply_overrides_button.config(state=tk.NORMAL if enabled else tk.DISABLED)
        self._clear_overrides_button.config(
            state=tk.NORMAL if enabled and any(entry.overrides for entry in entries) else tk.DISABLED
        )

    def _mark_override_dirty(self, key: str) -> None:
        if not self._detail_values_loading and self._ordered_samples:
            self._override_dirty_fields.add(key)

    def _current_override_value(self, key: str):
        raw = self._detail_widgets[key].get().strip()
        if raw == "":
            return None
        if key in ("base_pressure", "operating_pressure", "flow_sccm"):
            return float(raw)
        return raw

    def _commit_selected_overrides(self) -> bool:
        if not self._ordered_samples or not self._override_dirty_fields:
            return True
        entries = self._planned_selected_entries()
        if len(entries) != len(self._ordered_samples):
            self._override_dirty_fields.clear()
            return True
        flattened = sorted(self._override_conflict_fields & self._override_dirty_fields)
        if flattened and not messagebox.askyesno(
            "Flatten differing overrides",
            "Applying these fields will replace differing overrides on all selected samples: "
            + ", ".join(field.replace("_", " ") for field in flattened)
            + ". Continue?",
            parent=self._root,
        ):
            return False
        try:
            values = {key: self._current_override_value(key) for key in self._override_dirty_fields}
        except ValueError:
            self._status.set("Override pressures and flow must be valid numbers.")
            return False
        selected = set(self._ordered_samples)
        updated = []
        for entry in self._entries:
            if entry.sample not in selected:
                updated.append(entry)
                continue
            overrides = dict(entry.overrides)
            for key, value in values.items():
                if value is None:
                    overrides.pop(key, None)
                else:
                    overrides[key] = value
            updated.append(BatchPlanEntry(entry.sample, entry.mode, entry.target, overrides))
        self._entries = updated
        self._status.set("Applied selected sample overrides.")
        self._render_plan()
        self._show_override_editor()
        return True

    def _apply_selected_overrides(self) -> None:
        self._commit_selected_overrides()

    def _clear_selected_overrides(self) -> None:
        entries = self._planned_selected_entries()
        if len(entries) != len(self._ordered_samples):
            self._status.set("Assign plan targets to every selected sample before clearing overrides.")
            return
        if not messagebox.askyesno(
            "Clear selected overrides",
            "Clear all exposure-setting overrides for the selected samples? They will inherit the batch defaults.",
            parent=self._root,
        ):
            return
        selected = set(self._ordered_samples)
        self._entries = [
            BatchPlanEntry(entry.sample, entry.mode, entry.target) if entry.sample in selected else entry
            for entry in self._entries
        ]
        self._status.set("Cleared selected sample overrides.")
        self._render_plan()
        self._show_override_editor()

    def _render_generator_parameters(self) -> None:
        for child in self._generator_parameters.winfo_children():
            child.destroy()
        self._generator_parameters.columnconfigure(1, weight=1)
        if self._generator.get() == "Target assignment":
            ttk.Label(self._generator_parameters, text="Mode").grid(row=0, column=0, sticky=tk.W, padx=(0, 4))
            ttk.Combobox(
                self._generator_parameters,
                textvariable=self._target_mode,
                values=(TargetMode.DOSE.value, TargetMode.TIME.value),
                state="readonly",
                width=12,
            ).grid(row=0, column=1, sticky=tk.EW)
            ttk.Label(self._generator_parameters, text="Target").grid(row=1, column=0, sticky=tk.W, padx=(0, 4), pady=(6, 0))
            ttk.Entry(self._generator_parameters, textvariable=self._target_value).grid(row=1, column=1, sticky=tk.EW, pady=(6, 0))
        elif self._generator.get() == "Linear contrast dose":
            ttk.Label(self._generator_parameters, text="Minimum dose").grid(row=0, column=0, sticky=tk.W, padx=(0, 4))
            ttk.Entry(self._generator_parameters, textvariable=self._contrast_min_value).grid(row=0, column=1, sticky=tk.EW)
            ttk.Label(self._generator_parameters, text="Maximum dose").grid(row=1, column=0, sticky=tk.W, padx=(0, 4), pady=(6, 0))
            ttk.Entry(self._generator_parameters, textvariable=self._contrast_max_value).grid(row=1, column=1, sticky=tk.EW, pady=(6, 0))
            ttk.Label(
                self._generator_parameters,
                text="Assigns inclusive doses in the stage selection order.",
                wraplength=230,
            ).grid(row=2, column=0, columnspan=2, sticky=tk.W, pady=(6, 0))
        else:
            ttk.Label(
                self._generator_parameters,
                text="Adds zero-dose controls. Controls are recorded in the plan but do not start an exposure.",
                wraplength=230,
            ).grid(row=0, column=0, columnspan=2, sticky=tk.W)

    def _toggle_stage_sample(self, sample: int) -> None:
        was_selected = sample in self._ordered_samples
        try:
            selected_entries = self._planned_selected_entries()
            if was_selected:
                if len(selected_entries) == len(self._ordered_samples) and not self._commit_selected_overrides():
                    return
                self._ordered_samples.remove(sample)
            elif self._ordered_samples:
                if not self._commit_selected_overrides():
                    return
            else:
                self._capture_template_from_form()
        except ValueError as exc:
            self._status.set(f"Batch defaults are invalid: {exc}")
            return
        if not was_selected:
            self._ordered_samples.append(sample)
        if self._ordered_samples:
            self._show_override_editor()
        else:
            self._show_template_editor()
        self._render_stage()

    @staticmethod
    def _stage_override_text(entry: BatchPlanEntry) -> str:
        labels = {
            "name": "Name",
            "description": "Desc",
            "operator": "Op",
            "zr_filter": "Zr",
            "sample_type": "Type",
            "base_pressure": "Base",
            "operating_pressure": "Press",
            "flow_sccm": "Flow",
        }
        values = []
        for key, label in labels.items():
            if key not in entry.overrides:
                continue
            value = str(entry.overrides[key])
            values.append(f"{label}={value[:18]}{'...' if len(value) > 18 else ''}")
        return "\n".join(" | ".join(values[index:index + 2]) for index in range(0, len(values), 2))

    def _render_stage(self) -> None:
        canvas = self._stage_canvas
        if canvas is None:
            return
        canvas.delete("all")
        width = max(canvas.winfo_width(), 520)
        height = max(canvas.winfo_height(), 390)
        center_x = width * 0.56
        center_y = height * 0.5
        scale = min(width / 150.0, height / 128.0)
        outer_radius = 52 * scale
        canvas.create_oval(
            center_x - outer_radius,
            center_y - outer_radius,
            center_x + outer_radius,
            center_y + outer_radius,
            fill="#eaf3f7",
            outline="#536776",
            width=2,
        )
        for radius_mm in RING_RADII.values():
            radius = radius_mm * scale
            canvas.create_oval(center_x - radius, center_y - radius, center_x + radius, center_y + radius, outline="#9aa9b2")
        canvas.create_oval(center_x - 5, center_y - 5, center_x + 5, center_y + 5, fill="#4d87a2", outline="")
        canvas.create_text(30, center_y, text="DOOR", angle=90, fill="#b44740", font=("TkDefaultFont", 11, "bold"))
        entries_by_sample = {entry.sample: entry for entry in self._entries}
        order_by_sample = {sample: index for index, sample in enumerate(self._ordered_samples, start=1)}
        for sample, data in enumerate(self._samples):
            angle = math.radians(data["angle"])
            radius = data["radius"] * scale
            x = center_x + radius * math.cos(angle)
            y = center_y + radius * math.sin(angle)
            selected = sample in order_by_sample
            entry = entries_by_sample.get(sample)
            fill = "#70b8d6" if selected else "#d4e0e6" if entry is not None else "#f5f7f8"
            outline = "#166d91" if selected else "#55656f"
            tags = (f"batch-sample-{sample}",)
            if entry is not None and entry.overrides:
                canvas.create_text(
                    x,
                    max(8, y - 30),
                    text=self._stage_override_text(entry),
                    fill="#8a4b08",
                    width=125,
                    font=("TkDefaultFont", 7, "bold"),
                    tags=tags,
                )
            canvas.create_rectangle(x - 17, y - 14, x + 17, y + 14, fill=fill, outline=outline, width=2 if selected else 1, tags=tags)
            canvas.create_text(x, y, text=str(sample + 1), font=("TkDefaultFont", 9, "bold"), tags=tags)
            if selected:
                canvas.create_oval(x - 24, y - 22, x - 10, y - 8, fill="#1f6687", outline="", tags=tags)
                canvas.create_text(x - 17, y - 15, text=str(order_by_sample[sample]), fill="white", font=("TkDefaultFont", 7, "bold"), tags=tags)
            if entry is not None:
                target = "CTRL" if entry.is_control else f"{entry.target:g}{'d' if entry.mode is TargetMode.DOSE else 's'}"
                canvas.create_text(x, y + 24, text=target, fill="#315b70", font=("TkDefaultFont", 8, "bold"), tags=tags)
            canvas.tag_bind(f"batch-sample-{sample}", "<Button-1>", lambda _event, index=sample: self._toggle_stage_sample(index))
        if self._ordered_samples:
            order_text = ", ".join(f"{index}. Sample {sample + 1}" for index, sample in enumerate(self._ordered_samples, start=1))
            self._selection_order_text.set(f"Selection order: {order_text}")
        else:
            self._selection_order_text.set("Selection order: none")

    def _apply_generator(self) -> None:
        try:
            if not self._commit_selected_overrides():
                return
            name = self._generator.get()
            if name == "Linear contrast dose":
                generator = LinearContrastDoseGenerator(float(self._contrast_min_value.get()), float(self._contrast_max_value.get()))
            elif name == "Zero-dose control":
                generator = ControlGenerator()
            else:
                generator = TargetAssignmentGenerator(TargetMode(self._target_mode.get()), float(self._target_value.get()))
            application = apply_plan_generator(self._plan(), generator, tuple(self._ordered_samples))
            self._entries = list(application.plan.entries)
            assignments = ", ".join(f"S{entry.sample + 1}={entry.target:g} {entry.mode.value}" for entry in application.generated_entries)
            self._status.set(f"Applied {application.generator_name} in displayed order: {assignments}")
            self._render_plan()
            self._show_override_editor()
        except Exception as exc:
            self._status.set(f"Generator failed: {exc}")

    def _render_plan(self, selected=None) -> None:
        for item in self._plan_tree.get_children():
            self._plan_tree.delete(item)
        for index, entry in enumerate(self._entries):
            self._plan_tree.insert("", tk.END, iid=str(index), values=(index + 1, entry.sample + 1, entry.mode.value, entry.target))
        if selected is not None and 0 <= selected < len(self._entries):
            self._plan_tree.selection_set(str(selected))
        self._render_stage()

    def _remove_entry(self) -> None:
        selection = self._plan_tree.selection()
        if selection:
            self._entries.pop(int(selection[0]))
            self._render_plan()
            if self._ordered_samples:
                self._show_override_editor()

    def _move_entry(self, direction: int) -> None:
        selection = self._plan_tree.selection()
        if not selection:
            return
        index = int(selection[0])
        target = index + direction
        if not 0 <= target < len(self._entries):
            return
        self._entries[index], self._entries[target] = self._entries[target], self._entries[index]
        self._render_plan(target)

    def _command(self, command, args=None) -> None:
        self._status.set(f"Sending {command}...")
        self._client.request(command, args)

    def _save_new(self) -> None:
        try:
            if not self._commit_selected_overrides():
                return
            plan = batch_plan_to_dict(self._plan())
        except Exception as exc:
            self._status.set(f"Plan is invalid: {exc}")
            return
        self._command("create", {"plan": plan, "origin": "tk_gui"})

    def _save_revision(self) -> None:
        if self._manifest_uuid is None or self._manifest_revision is None:
            self._status.set("Load a saved manifest first.")
            return
        if not self._commit_selected_overrides():
            return
        self._command(
            "update",
            {
                "batch_uuid": self._manifest_uuid,
                "expected_revision": self._manifest_revision,
                "plan": batch_plan_to_dict(self._plan()),
                "revision_note": "Updated in Tk GUI",
            },
        )

    def _selected_manifest_uuid(self) -> str | None:
        selection = self._manifest_tree.selection()
        return selection[0] if selection else None

    @staticmethod
    def _set_button_enabled(button, enabled: bool) -> None:
        button.config(state=tk.NORMAL if enabled else tk.DISABLED)

    def _render_manifest_inventory(self) -> None:
        selected = self._selected_manifest_uuid()
        for item in self._manifest_tree.get_children():
            self._manifest_tree.delete(item)
        draft_or_active_only = self._manifest_filter.get() == "Draft / active"
        for manifest in self._manifests_by_uuid.values():
            if draft_or_active_only and manifest.get("status") not in ("draft", "active"):
                continue
            self._manifest_tree.insert(
                "",
                tk.END,
                iid=manifest["batch_uuid"],
                values=(manifest["status"], manifest["revision"], manifest["name"]),
            )
        if selected and self._manifest_tree.exists(selected):
            self._manifest_tree.selection_set(selected)

    def _update_control_states(self) -> None:
        state = self._state or {}
        connected = self._state is not None
        active = state.get("active_manifest")
        selected_uuid = self._selected_manifest_uuid()
        selected = self._manifests_by_uuid.get(selected_uuid) if selected_uuid else None
        selected_status = selected.get("status") if selected else None
        loaded = self._manifests_by_uuid.get(self._manifest_uuid) if self._manifest_uuid else None
        loaded_status = loaded.get("status") if loaded else None
        active_uuid = active.get("batch_uuid") if isinstance(active, dict) else None
        active_paused = bool(active.get("paused")) if isinstance(active, dict) else False
        cancelling = bool(active.get("cancel_pending")) if isinstance(active, dict) else False
        active_mode = state.get("execution_mode")
        phase = str(state.get("phase", ""))

        self._set_button_enabled(self._load_button, connected and selected is not None)
        self._set_button_enabled(self._new_button, True)
        self._set_button_enabled(self._save_new_button, connected)
        self._set_button_enabled(
            self._save_revision_button,
            connected
            and self._manifest_uuid is not None
            and (
                loaded_status in ("draft", "submitted")
                or (
                    loaded_status == "active"
                    and active_uuid == self._manifest_uuid
                    and phase in ("waiting_continue", "paused", "restart_paused")
                )
            ),
        )
        self._set_button_enabled(
            self._activate_button,
            connected and active is None and selected_status in ("draft", "submitted", "cancelled"),
        )
        self._set_button_enabled(self._withdraw_button, connected and selected_status in ("draft", "submitted"))
        self._set_button_enabled(self._restore_button, connected and selected_status == "withdrawn")

        self._set_button_enabled(
            self._continue_button,
            connected and active is not None and active_mode == ExecutionMode.MANUAL.value and not active_paused and phase == "waiting_continue",
        )
        self._set_button_enabled(self._pause_button, connected and active is not None and not active_paused and not cancelling)
        self._set_button_enabled(
            self._resume_button,
            connected
            and active is not None
            and not cancelling
            and (active_paused or phase in ("failure_paused", "error_paused")),
        )
        self._set_button_enabled(self._cancel_button, connected and active is not None and not cancelling)
        self._set_button_enabled(self._refresh_button, connected)
        self._set_button_enabled(self._manual_mode_button, connected)
        self._set_button_enabled(self._automatic_mode_button, connected)

    def _set_execution_mode(self) -> None:
        if self._state is not None:
            self._command("set_mode", {"mode": self._mode.get()})

    def _load_selected(self) -> None:
        batch_uuid = self._selected_manifest_uuid()
        if batch_uuid:
            self._command("get_manifest", {"batch_uuid": batch_uuid})

    def _activate(self) -> None:
        batch_uuid = self._selected_manifest_uuid()
        if batch_uuid:
            self._command("activate", {"batch_uuid": batch_uuid})

    def _withdraw(self) -> None:
        batch_uuid = self._selected_manifest_uuid()
        if batch_uuid:
            self._command("withdraw", {"batch_uuid": batch_uuid})

    def _unwithdraw(self) -> None:
        batch_uuid = self._selected_manifest_uuid()
        if batch_uuid:
            self._command("unwithdraw", {"batch_uuid": batch_uuid})

    def _new_draft(self) -> None:
        self._manifest_uuid = None
        self._manifest_revision = None
        self._entries = []
        self._ordered_samples = []
        self._render_plan()
        self._show_template_editor()
        self._status.set("New local draft.")
        self._update_control_states()

    def _load_manifest(self, value) -> None:
        self._manifest_uuid = value["batch_uuid"]
        self._manifest_revision = value["revision"]
        plan = batch_plan_from_dict(value["plan"])
        self._entries = list(plan.entries)
        self._ordered_samples = []
        self._template_value = plan.template
        self._show_template_editor()
        self._render_plan()
        self._status.set(f"Loaded {self._manifest_uuid}, revision {self._manifest_revision}.")
        self._update_control_states()

    def _render_state(self, state) -> None:
        self._manifests_by_uuid = {
            manifest["batch_uuid"]: manifest
            for manifest in state.get("manifests", [])
            if isinstance(manifest, dict) and "batch_uuid" in manifest
        }
        self._render_manifest_inventory()
        active = state.get("active_manifest")
        decision = (state.get("assessment") or {}).get("decision") or {}
        phase = str(state.get("phase", "unknown"))
        detail = state.get("message") or ""
        next_action = decision.get("message") or ""
        self._status_phase.set(phase.replace("_", " ").upper())
        tone = self._status_tone(phase)
        self._status_phase_label.configure(style=f"BatchStatus.{tone}.TLabel")
        self._status_action_label.configure(style=f"BatchStatus.{tone}.TLabel")
        if active:
            self._status_batch.set(
                f"{active['plan']['template']['name']} | {state.get('execution_mode', 'manual')} | revision {active['revision']}"
            )
        else:
            self._status_batch.set("No active batch")
        self._status_action.set(detail)
        self._status_next.set("Next: " + next_action if next_action and next_action != detail else "")
        progress = (state.get("assessment") or {}).get("progress") or []
        remaining = sum(1 for item in progress if item.get("state") not in ("within_tolerance", "overshot"))
        self._status_remaining.set(f"Remaining samples: {remaining}" if active else "Remaining: —")
        for item in self._status_progress.get_children():
            self._status_progress.delete(item)
        for item in progress:
            mode = item.get("mode", "")
            actual = item.get("cumulative_dose") if mode == "dose" else item.get("cumulative_runtime")
            unit = "mJ/cm2" if mode == "dose" else "s"
            progress_state = str(item.get("state", ""))
            self._status_progress.insert(
                "",
                tk.END,
                values=(
                    item.get("sample_number"),
                    f"{item.get('target', 0):g} {unit}",
                    f"{actual or 0:g} {unit}",
                    f"{item.get('remainder', 0):g} {unit}",
                    progress_state.replace("_", " "),
                ),
                tags=(f"progress-{progress_state}",),
            )
        detail_for_bar = next_action or detail
        self._status.set(f"{phase}: {detail_for_bar}")
        execution_mode = state.get("execution_mode")
        if execution_mode in (ExecutionMode.MANUAL.value, ExecutionMode.AUTOMATIC.value):
            self._mode.set(execution_mode)
        if active and active.get("batch_uuid") == self._manifest_uuid:
            self._manifest_revision = active["revision"]
        self._update_control_states()

    @staticmethod
    def _status_tone(phase: str) -> str:
        if phase in ("error", "error_paused", "failure_paused", "start_error"):
            return "Error"
        if phase in ("paused", "restart_paused", "waiting_continue", "cancelling"):
            return "Warning"
        if phase in ("completed",):
            return "Good"
        if phase in ("starting", "wait_active", "wait_controller", "wait_finalization", "repair_metrics"):
            return "Active"
        return "Neutral"

    def _update(self) -> None:
        for kind, payload in self._client.pop_messages():
            if kind == "state":
                self._state = payload
                self._render_state(payload)
            elif kind == "result":
                command, result = payload
                if command in ("create", "update", "get_manifest", "unwithdraw") and isinstance(result, dict):
                    self._load_manifest(result)
                else:
                    self._status.set(f"{command} completed.")
                    self._update_control_states()
            elif kind == "error":
                self._status.set(str(payload))
            elif kind == "connected":
                self._status.set("Connected; refreshing state...")
        self._root.after(200, self._update)

    def close(self) -> None:
        self._client.close()
        self._presets.close()


def main() -> None:
    import os

    root = tk.Tk()
    gui = BatchControllerGUI(root, os.path.join(os.environ["EUVL_PATH"], "datasets"))
    root.protocol("WM_DELETE_WINDOW", lambda: (gui.close(), root.destroy()))
    root.mainloop()


if __name__ == "__main__":
    main()