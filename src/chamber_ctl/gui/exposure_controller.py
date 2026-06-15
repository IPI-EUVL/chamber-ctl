import os
import tkinter as tk
from tkinter import ttk
from tkinter import messagebox
import math
import pickle
import uuid
import segment_bytes

from ipi_ecs.core.tcp import TCPClientSocket
from ipi_ecs.dds import client, subsystem, types, magics
from ipi_ecs.logging.client import LogClient
from ipi_ecs.gui.experiment_controller_gui import ExperimentInterface, ExperimentControllerGUI
from ipi_ecs.cli.captive_cli import wait_for

from chamber_ctl import ECS_IP, ECS_PORT
from chamber_ctl.gui.sample_motion_gui import draw_sample_stage, build_sample_data, RING_RADII
from chamber_ctl.subsystems import uuids
from chamber_ctl.subsystems.exposure_controller import ExposureSettings
from chamber_ctl.subsystems.laser import LaserSyncStatus
from chamber_ctl.subsystems.settings_presets import SettingsPresets
import chamber_ctl.subsystems.sample_motion as stage_client


class ExposureControllerGUI():
    def __init__(self, root, own_window: bool = True):
        self.__own_window = own_window
        self.__exp_itf = ExperimentInterface("exposure", UUID_EXPOSURE_CONTROLLER, exp_settings_type=ExposureSettings)
        self.__exp_ctl = ExperimentControllerGUI(root, self.__exp_itf, own_window=own_window)
        self.__root = root

        self.__samples = build_sample_data()

        self.__sample_options = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]
        self.__zr_filter_options = []
        self.__sample_type_options = []
        self.__operator_options = []
        
        db_path = os.path.join(os.environ["EUVL_PATH"], "datasets")
        self.__presets = SettingsPresets(db_path)

        self.__reload_presets()

        self.__selected_sample = 1

        # Target type tracking
        self.__target_type_var = tk.StringVar(value="time")

        # Widget references for reading values
        self.__target_input = None
        self.__target_type_label = None
        self.__name_input = None
        self.__description_input = None
        self.__operator_combo = None
        self.__zr_filter_combo = None
        self.__sample_combo = None
        self.__sample_type_combo = None
        self.__base_pressure_input = None
        self.__operating_pressure_input = None
        self.__flowrate_input = None
        self.__settings_frame = None
        self.__time_radio = None
        self.__dose_radio = None
        self.__refresh_button = None
        self.__apply_button = None
        self.__settings_locked = False

        c_uuid = uuid.uuid4()
        s_uuid = uuid.uuid4()

        self.__logger_sock = TCPClientSocket()

        self.__logger_sock.connect(("127.0.0.1", 11751))
        self.__logger_sock.start()

        self.__logger = LogClient(self.__logger_sock, origin_uuid=c_uuid)

        self.__did_config = False
        self.__subsystem = None

        self.__position_kv = None

        self.__dose_kv = None
        self.__time_kv = None
        self.__laser_status_kv = None
        self.__target_status_kv = None

        self.__status_dose_value = None
        self.__status_time_value = None
        self.__status_laser_value = None
        self.__status_chopper_value = None
        self.__status_target_value = None
        self.__status_target_time_value = None
        self.__status_chopper_phase_value = None
        self.__laser_status_canvas = None
        self.__laser_status_canvas_text = None
        self.__laser_canvas_flash = False

        self.__queue_add_kv = None
        self.__queue_kv = None
        self.__queue_start_event = None

        self.__queue_list = []
        self.__queue_listbox = None
        self.__queue_count_label = None
        self.__queue_status_label = None

        def _on_ready():
            if self.__did_config:
                return
            
            self.__did_config = True
            sh = self.__client.register_subsystem(f"__cli_{s_uuid}", s_uuid, temporary=True)

            self.__on_got_subsystem(sh)

        #print("Registering subsystem...")
        self.__client = client.DDSClient(c_uuid, logger=self.__logger, ip=ECS_IP)
        self.__client.when_ready().then(_on_ready)

        self.initialize_component()

        self.redraw_gui()
        self.sync_settings()
        self.__update_live_status()

    def __reload_presets(self):
        try:
            self.__zr_filter_options = self.__presets.read_zr_filters()
            self.__sample_type_options = self.__presets.read_sample_types()
            self.__operator_options = self.__presets.read_operators()
        except Exception as e:
            messagebox.showerror("Exposure Controller", f"Failed to load settings presets:\n{e}")

    def __refresh_options(self):
        """Refresh dropdown options from their source data."""
        self.__reload_presets()
        # Update operator dropdown
        if self.__operator_combo is not None:
            self.__operator_combo.config(values=self.__operator_options)

        # Update zr filter dropdown
        if self.__zr_filter_combo is not None:
            self.__zr_filter_combo.config(values=self.__zr_filter_options)

        # Update sample dropdown
        if self.__sample_combo is not None:
            self.__sample_combo.config(values=self.__sample_options)

        # Update sample type dropdown
        if self.__sample_type_combo is not None:
            self.__sample_type_combo.config(values=self.__sample_type_options)

    def __update_target_unit_label(self, *args):
        """Update the unit label based on target type selection."""
        if self.__target_type_label is not None:
            target_type = self.__target_type_var.get()
            if target_type == "time":
                self.__target_type_label.config(text="seconds")
            else:
                self.__target_type_label.config(text="dose")

    def __on_got_subsystem(self, handle: client._RegisteredSubsystemHandle):
        self.__position_kv = handle.add_remote_kv(
            uuids.UUID_SAMPLE_MOTION_CONTROLLER,
            subsystem.KVDescriptor(types.VectorTypeSpecifier(types.FloatTypeSpecifier(), 2), b"position", True, True, False)
        )

        self.__queue_add_kv = handle.add_remote_kv(
            uuids.UUID_EXPERIMENT_QUEUE_CONTROLLER,
            subsystem.KVDescriptor(types.ByteTypeSpecifier(), b"add_to_queue", False, False, True),
        )
        self.__queue_kv = handle.add_remote_kv(
            uuids.UUID_EXPERIMENT_QUEUE_CONTROLLER,
            subsystem.KVDescriptor(types.ByteTypeSpecifier(), b"queue", False, True, True),
        )
        self.__queue_start_event = handle.add_event_provider(b"queue_start_exposure")

        self.__dose_kv = handle.add_remote_kv(
            uuids.UUID_OSCILLOSCOPE_CONTROLLER,
            subsystem.KVDescriptor(types.FloatTypeSpecifier(), b"cur_dose", True, True, False)
        )

        self.__time_kv = handle.add_remote_kv(
            uuids.UUID_OSCILLOSCOPE_CONTROLLER,
            subsystem.KVDescriptor(types.FloatTypeSpecifier(), b"cur_time", True, True, False)
        )

        self.__laser_status_kv = handle.add_remote_kv(
            uuids.UUID_LASER_CONTROLLER,
            subsystem.KVDescriptor(types.ByteTypeSpecifier(), b"status", True, True, False),
        )

        self.__target_status_kv = handle.add_remote_kv(
            uuids.UUID_TARGET_CONTROLLER,
            subsystem.KVDescriptor(types.VectorTypeSpecifier(types.ByteTypeSpecifier(), 2), b"status", True, True, False),
        )

        self.__refresh_queue_from_remote(update_status=False)

    @staticmethod
    def __format_active_status(is_active: bool, is_warming: bool, warm_label: str) -> str:
        if not is_active:
            return "OFF"
        if is_warming:
            return warm_label
        return "Active"

    @staticmethod
    def __format_target_status(state) -> str:
        if state is None:
            return "Unknown"

        if getattr(state, "is_homing", False):
            return "Homing"
        if getattr(state, "is_jogging", False):
            return "Jogging"
        if getattr(state, "is_moving_to_start", False):
            return "Moving to Start"
        if getattr(state, "is_running", False):
            return "Running"
        return "Idle"

    def __update_live_status(self):
        laser_status = None
        if self.__status_dose_value is not None:
            dose_value = self.__dose_kv.value if self.__dose_kv is not None else None
            self.__status_dose_value.config(text="N/A" if dose_value is None else f"{float(dose_value):.2f} mJ/cm²")

        if self.__status_time_value is not None:
            time_value = self.__time_kv.value if self.__time_kv is not None else None
            self.__status_time_value.config(text="N/A" if time_value is None else f"{float(time_value):.2f} s")

        if self.__status_laser_value is not None:
            laser_text = "Unknown"
            if self.__laser_status_kv is not None and self.__laser_status_kv.value is not None:
                try:
                    laser_payload = self.__laser_status_kv.value
                    if isinstance(laser_payload, list):
                        laser_payload = bytes(laser_payload)
                    laser_status = LaserSyncStatus.decode(laser_payload)
                    laser_text = self.__format_active_status(laser_status.laser_on, laser_status.laser_warming_up, "Warming Up")
                except Exception:
                    laser_text = "Unknown"
            self.__status_laser_value.config(text=laser_text)

        if self.__status_chopper_value is not None:
            chopper_text = "Unknown"
            if laser_status is None and self.__laser_status_kv is not None and self.__laser_status_kv.value is not None:
                try:
                    laser_payload = self.__laser_status_kv.value
                    if isinstance(laser_payload, list):
                        laser_payload = bytes(laser_payload)
                    laser_status = LaserSyncStatus.decode(laser_payload)
                except Exception:
                    laser_status = None
            if laser_status is not None:
                chopper_text = self.__format_active_status(laser_status.chopper_on, laser_status.chopper_starting_up, "Starting Up")
            self.__status_chopper_value.config(text=chopper_text)

        if self.__status_chopper_phase_value is not None:
            if laser_status is not None:
                self.__status_chopper_phase_value.config(
                    text=f"Phase: current={float(laser_status.current_phase):.3f} target={float(laser_status.target_phase):.3f}"
                )
            else:
                self.__status_chopper_phase_value.config(text="Phase: current=N/A target=N/A")

        if self.__status_target_value is not None:
            target_text = "Unknown"
            if self.__target_status_kv is not None and self.__target_status_kv.value is not None:
                try:
                    target_value = self.__target_status_kv.value
                    if isinstance(target_value, (tuple, list)) and len(target_value) >= 1:
                        target_payload = target_value[0]
                        if not isinstance(target_payload, (bytes, bytearray)):
                            target_payload = bytes(target_payload)
                        target_state = pickle.loads(target_payload)
                        target_text = self.__format_target_status(target_state)
                except Exception:
                    target_text = "Unknown"
            self.__status_target_value.config(text=target_text)

        if self.__status_target_time_value is not None:
            if self.__target_status_kv is not None and self.__target_status_kv.value is not None:
                try:
                    target_value = self.__target_status_kv.value
                    if isinstance(target_value, (tuple, list)) and len(target_value) >= 1:
                        target_payload = target_value[0]
                        if not isinstance(target_payload, (bytes, bytearray)):
                            target_payload = bytes(target_payload)
                        target_state = pickle.loads(target_payload)
                        self.__status_target_time_value.config(
                            text=f"Time: t={float(getattr(target_state, 'current_time', 0.0)):.3f}s seg={int(getattr(target_state, 'current_segment', 0))}"
                        )
                    else:
                        self.__status_target_time_value.config(text="Time: t=N/A seg=N/A")
                except Exception:
                    self.__status_target_time_value.config(text="Time: t=N/A seg=N/A")
            else:
                self.__status_target_time_value.config(text="Time: t=N/A seg=N/A")

        if self.__laser_status_canvas is not None:
            self.__update_laser_canvas(laser_status)

        if self.__root is not None:
            self.__root.after(250, self.__update_live_status)

    def __update_laser_canvas(self, laser_status):
        if self.__laser_status_canvas is None:
            return

        self.__laser_status_canvas.delete("all")
        self.__laser_status_canvas.config(bg="black")

        if laser_status is None:
            text = "LASER UNKNOWN"
            color = "yellow"
        elif not laser_status.laser_on:
            text = "LASER OFF"
            color = "green"
        else:
            text = "LASER ACTIVE"
            color = "red" if self.__laser_canvas_flash else "yellow"
            self.__laser_canvas_flash = not self.__laser_canvas_flash

        width = int(self.__laser_status_canvas.winfo_width() or 220)
        height = int(self.__laser_status_canvas.winfo_height() or 60)
        self.__laser_status_canvas.create_text(
            width // 2,
            height // 2,
            text=text,
            fill=color,
            font=("TkDefaultFont", 25, "bold"),
        )

    def __get_current_settings(self) -> dict[str, str]:
        """Collect current settings from all input widgets."""
        settings = {}

        # Name and description
        settings["name"] = self.__name_input.get()
        settings["description"] = self.__description_input.get("1.0", tk.END).strip()

        # Target value - use either target_time or target_dose based on selection
        target_value = self.__target_input.get()
        target_type = self.__target_type_var.get()
        if target_type == "time":
            settings["target_time"] = target_value
            settings["target_dose"] = "0"  # Default value
        else:
            settings["target_dose"] = target_value
            settings["target_time"] = "0"  # Default value

        # Operator
        settings["operator"] = self.__operator_combo.get()

        # Zr filter
        settings["zr_filter"] = self.__zr_filter_combo.get()

        # Sample
        settings["sample"] = str(int(self.__sample_combo.get()) - 1)

        # Sample type
        settings["sample_type"] = self.__sample_type_combo.get()

        # Pressures
        settings["base_pressure"] = self.__base_pressure_input.get()

        settings["operating_pressure"] = self.__operating_pressure_input.get()

        # Flowrate
        settings["flow_sccm"] = self.__flowrate_input.get()

        return settings

    def __on_apply_settings(self):
        """Apply current settings to the experiment controller."""
        settings = self.__get_current_settings()
        self.__exp_ctl.do_update_settings(settings)

    @staticmethod
    def __settings_to_bytes(settings: dict[str, str]) -> bytes:
        obj = ExposureSettings()
        for key, value in settings.items():
            obj.set_attr(key, value)
        return obj.encode().encode("utf-8")

    def __get_current_settings_bytes(self) -> bytes:
        return self.__settings_to_bytes(self.__get_current_settings())

    def __set_entry_text(self, entry: ttk.Entry, value) -> None:
        entry.delete(0, tk.END)
        entry.insert(0, "" if value is None else str(value))

    def __set_text_text(self, text_widget: tk.Text, value) -> None:
        text_widget.delete("1.0", tk.END)
        if value is not None:
            text_widget.insert("1.0", str(value))

    def __ensure_combobox_value(self, combobox: ttk.Combobox, value: str) -> None:
        values = list(combobox.cget("values"))
        if value != "" and value not in values:
            values.append(value)
            combobox.config(values=values)
        combobox.set(value)

    def __sync_settings_from_experiment(self):
        exp = self.__exp_itf.get_experiment()
        if exp is None:
            return

        self.__apply_settings_to_form(exp.get_settings().get_dict())

    def __apply_settings_to_form(self, settings: dict):
        if settings is None:
            return

        self.__set_entry_text(self.__name_input, settings.get("name", ""))
        self.__set_text_text(self.__description_input, settings.get("description", ""))

        target_time = settings.get("target_time", "")
        target_dose = settings.get("target_dose", "")

        target_type = "time"
        target_value = target_time
        try:
            if float(target_dose) > 0:
                target_type = "dose"
                target_value = target_dose
        except (TypeError, ValueError):
            if target_dose not in [None, "", "0", "0.0"]:
                target_type = "dose"
                target_value = target_dose

        self.__target_type_var.set(target_type)
        self.__update_target_unit_label()
        self.__set_entry_text(self.__target_input, target_value)

        self.__ensure_combobox_value(self.__operator_combo, str(settings.get("operator", "")))
        self.__ensure_combobox_value(self.__zr_filter_combo, str(settings.get("zr_filter", "")))
        self.__ensure_combobox_value(self.__sample_type_combo, str(settings.get("sample_type", "")))

        sample_value = settings.get("sample", "")
        sample_display = ""
        try:
            sample_display = str(int(sample_value) + 1)
        except (TypeError, ValueError):
            if sample_value is not None:
                sample_display = str(sample_value)
        self.__ensure_combobox_value(self.__sample_combo, sample_display)

        self.__set_entry_text(self.__base_pressure_input, settings.get("base_pressure", ""))
        self.__set_entry_text(self.__operating_pressure_input, settings.get("operating_pressure", ""))
        self.__set_entry_text(self.__flowrate_input, settings.get("flow_sccm", ""))

    @staticmethod
    def __summarize_queue_item(index: int, b_item: bytes) -> str:
        try:
            settings = ExposureSettings.decode(b_item.decode("utf-8")).get_dict()
        except Exception:
            return f"{index + 1:02d}. <invalid queue item>"

        name = str(settings.get("name", "")).strip() or "(unnamed)"
        operator = str(settings.get("operator", "")).strip() or "-"
        sample = str(int(settings.get("sample", 0)) + 1).strip() or "-"

        target_label = "sec"
        target_value = settings.get("target_time", 0)
        try:
            if float(settings.get("target_dose", 0)) > 0:
                target_label = "mj/cm²"
                target_value = settings.get("target_dose", 0)
        except (TypeError, ValueError):
            if str(settings.get("target_dose", "")).strip() not in ("", "0", "0.0"):
                target_label = "mj/cm²"
                target_value = settings.get("target_dose", "")

        return f"{index + 1:02d}. {name} | {target_label}={target_value} | op={operator} | sample={sample}"

    def __queue_set_status(self, text: str):
        if self.__queue_status_label is not None:
            self.__queue_status_label.config(text=text)

    def __refresh_queue_listbox(self):
        if self.__queue_listbox is None:
            return

        self.__queue_listbox.delete(0, tk.END)
        for idx, b_item in enumerate(self.__queue_list):
            self.__queue_listbox.insert(tk.END, self.__summarize_queue_item(idx, b_item))

        if self.__queue_count_label is not None:
            self.__queue_count_label.config(text=f"Queue items: {len(self.__queue_list)}")

    def __decode_queue_blob(self, b_payload: bytes) -> list[bytes]:
        if b_payload is None or len(b_payload) == 0:
            return []

        items = list(segment_bytes.decode(b_payload))
        out = []
        for item in items:
            if not isinstance(item, bytes):
                raise ValueError("Queue item payload must be bytes.")
            ExposureSettings.decode(item.decode("utf-8"))
            out.append(item)

        return out

    def __refresh_queue_from_remote(self, update_status: bool = True):
        if self.__queue_kv is None:
            if update_status:
                self.__queue_set_status("Queue subsystem not connected.")
            return

        awaiter = self.__queue_kv.try_get(client.KVP_RET_AWAIT)
        if awaiter is None:
            if update_status:
                self.__queue_set_status("Failed to request queue read.")
            return

        try:
            value, _, _ = wait_for(awaiter, timeout=5.0)
            self.__queue_list = self.__decode_queue_blob(value)
            self.__refresh_queue_listbox()
            if update_status:
                self.__queue_set_status("Queue refreshed.")
        except Exception as exc:
            if update_status:
                self.__queue_set_status(f"Queue refresh failed: {exc}")

    def __on_queue_refresh(self):
        self.__refresh_queue_from_remote(update_status=True)

    def __on_queue_add_current(self):
        try:
            b_settings = self.__get_current_settings_bytes()
        except Exception as exc:
            self.__queue_set_status(f"Cannot encode current settings: {exc}")
            return

        self.__queue_list.append(b_settings)
        self.__refresh_queue_listbox()
        self.__queue_set_status("Added current settings to local queue staging.")

    def __on_queue_remove_selected(self):
        if self.__queue_listbox is None:
            return

        sel = self.__queue_listbox.curselection()
        if not sel:
            self.__queue_set_status("Select a queued item first.")
            return

        idx = sel[0]
        if idx < 0 or idx >= len(self.__queue_list):
            return

        self.__queue_list.pop(idx)
        self.__refresh_queue_listbox()
        self.__queue_set_status("Removed selected staged item.")

    def __on_queue_move_selected(self, direction: int):
        if self.__queue_listbox is None:
            return

        sel = self.__queue_listbox.curselection()
        if not sel:
            self.__queue_set_status("Select a queued item first.")
            return

        idx = sel[0]
        new_idx = idx + direction
        if new_idx < 0 or new_idx >= len(self.__queue_list):
            return

        self.__queue_list[idx], self.__queue_list[new_idx] = self.__queue_list[new_idx], self.__queue_list[idx]
        self.__refresh_queue_listbox()
        self.__queue_listbox.selection_set(new_idx)
        self.__queue_set_status("Reordered staged queue.")

    def __on_queue_load_selected(self):
        if self.__queue_listbox is None:
            return

        sel = self.__queue_listbox.curselection()
        if not sel:
            self.__queue_set_status("Select a queued item first.")
            return

        idx = sel[0]
        if idx < 0 or idx >= len(self.__queue_list):
            return

        try:
            settings = ExposureSettings.decode(self.__queue_list[idx].decode("utf-8")).get_dict()
            self.__apply_settings_to_form(settings)
            self.__queue_set_status("Loaded selected staged item into settings form.")
        except Exception as exc:
            self.__queue_set_status(f"Failed to load staged item: {exc}")

    def __on_queue_replace_selected(self):
        if self.__queue_listbox is None:
            return

        sel = self.__queue_listbox.curselection()
        if not sel:
            self.__queue_set_status("Select a queued item first.")
            return

        idx = sel[0]
        if idx < 0 or idx >= len(self.__queue_list):
            return

        try:
            self.__queue_list[idx] = self.__get_current_settings_bytes()
        except Exception as exc:
            self.__queue_set_status(f"Cannot encode current settings: {exc}")
            return

        self.__refresh_queue_listbox()
        self.__queue_listbox.selection_set(idx)
        self.__queue_set_status("Replaced selected staged item with current settings.")

    def __on_queue_commit(self):
        if self.__queue_kv is None:
            self.__queue_set_status("Queue subsystem not connected.")
            return

        payload = segment_bytes.encode(self.__queue_list)
        awaiter = self.__queue_kv.try_set(payload, client.KVP_RET_AWAIT)
        if awaiter is None:
            self.__queue_set_status("Queue commit request failed to send.")
            return

        try:
            _, state, reason = wait_for(awaiter, timeout=5.0)
            if state is not None:
                msg = reason.decode("utf-8", errors="replace") if isinstance(reason, bytes) else str(reason)
                self.__queue_set_status(f"Queue commit rejected: {msg}")
                return

            self.__queue_set_status("Queue committed to subsystem.")
        except Exception as exc:
            self.__queue_set_status(f"Queue commit failed: {exc}")

    def __on_queue_start(self):
        if self.__queue_start_event is None:
            self.__queue_set_status("Queue start event is unavailable.")
            return

        handle = self.__queue_start_event.call(bytes(), [uuids.UUID_EXPERIMENT_QUEUE_CONTROLLER])
        if handle is None:
            self.__queue_set_status("Failed to send queue start event.")
            return

        self.__queue_set_status("Queue start requested.")

    def __set_settings_controls_enabled(self, enabled: bool):
        entry_state = tk.NORMAL if enabled else tk.DISABLED
        text_state = tk.NORMAL if enabled else tk.DISABLED
        combo_state = "readonly" if enabled else tk.DISABLED
        button_state = tk.NORMAL if enabled else tk.DISABLED

        for widget in [
            self.__name_input,
            self.__target_input,
            self.__base_pressure_input,
            self.__operating_pressure_input,
            self.__flowrate_input,
        ]:
            if widget is not None:
                widget.config(state=entry_state)

        if self.__description_input is not None:
            self.__description_input.config(state=text_state)

        for widget in [
            self.__operator_combo,
            self.__zr_filter_combo,
            self.__sample_combo,
            self.__sample_type_combo,
        ]:
            if widget is not None:
                widget.config(state=combo_state)

        for widget in [self.__time_radio, self.__dose_radio, self.__refresh_button, self.__apply_button]:
            if widget is not None:
                widget.config(state=button_state)

    def __sync_settings_from_run_state(self):
        has_experiment = self.__exp_itf.get_experiment() is not None
        if has_experiment:
            self.__sync_settings_from_experiment()

        should_lock = has_experiment
        if should_lock != self.__settings_locked:
            self.__settings_locked = should_lock
            self.__set_settings_controls_enabled(not should_lock)

    def initialize_component(self):
        # Main container with two sections: canvas and settings
        main_container = ttk.Frame(self.__root)
        main_container.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # Left side: Sample visualization canvas
        stage_container = ttk.LabelFrame(main_container, text="Sample selection")
        stage_container.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 5))
        stage_container.columnconfigure(0, weight=1)
        stage_container.rowconfigure(0, weight=1)

        self.__canvas = tk.Canvas(stage_container, width=500, height=300, bg="white")
        self.__canvas.grid(row=0, column=0, sticky=tk.NSEW, padx=4, pady=4)

        queue_section = ttk.LabelFrame(stage_container, text="Experiment Queue")
        queue_section.grid(row=1, column=0, sticky=tk.EW, padx=4, pady=(0, 4))
        queue_section.columnconfigure(0, weight=1)

        queue_info = ttk.Frame(queue_section)
        queue_info.grid(row=0, column=0, sticky=tk.EW, padx=6, pady=(6, 2))
        queue_info.columnconfigure(0, weight=1)

        self.__queue_count_label = ttk.Label(queue_info, text="Queue items: 0")
        self.__queue_count_label.grid(row=0, column=0, sticky=tk.W)

        self.__queue_status_label = ttk.Label(queue_info, text="Queue ready.")
        self.__queue_status_label.grid(row=1, column=0, sticky=tk.W)

        queue_body = ttk.Frame(queue_section)
        queue_body.grid(row=1, column=0, sticky=tk.EW, padx=6, pady=(0, 6))
        queue_body.columnconfigure(0, weight=1)

        self.__queue_listbox = tk.Listbox(queue_body, height=8)
        self.__queue_listbox.grid(row=0, column=0, sticky=tk.EW)

        queue_buttons = ttk.Frame(queue_body)
        queue_buttons.grid(row=0, column=1, sticky=tk.NS, padx=(8, 0))

        ttk.Button(queue_buttons, text="Refresh", command=self.__on_queue_refresh).pack(fill=tk.X, pady=(0, 4))
        ttk.Button(queue_buttons, text="Add Current", command=self.__on_queue_add_current).pack(fill=tk.X, pady=(0, 4))
        ttk.Button(queue_buttons, text="Load Selected", command=self.__on_queue_load_selected).pack(fill=tk.X, pady=(0, 4))
        ttk.Button(queue_buttons, text="Replace Selected", command=self.__on_queue_replace_selected).pack(fill=tk.X, pady=(0, 4))
        ttk.Button(queue_buttons, text="Move Up", command=lambda: self.__on_queue_move_selected(-1)).pack(fill=tk.X, pady=(0, 4))
        ttk.Button(queue_buttons, text="Move Down", command=lambda: self.__on_queue_move_selected(1)).pack(fill=tk.X, pady=(0, 4))
        ttk.Button(queue_buttons, text="Remove", command=self.__on_queue_remove_selected).pack(fill=tk.X, pady=(0, 4))
        ttk.Button(queue_buttons, text="Commit", command=self.__on_queue_commit).pack(fill=tk.X, pady=(0, 4))
        ttk.Button(queue_buttons, text="Start Queue", command=self.__on_queue_start).pack(fill=tk.X)

        right_container = ttk.Frame(main_container)
        right_container.pack(side=tk.RIGHT, fill=tk.BOTH, expand=False)

        settings_container = ttk.LabelFrame(right_container, text="Experiment Settings")
        settings_container.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 8))

        status_container = ttk.LabelFrame(right_container, text="Current Status")
        status_container.pack(side=tk.RIGHT, fill=tk.Y, expand=False)

        status_grid = ttk.Frame(status_container, padding=10)
        status_grid.pack(fill=tk.BOTH, expand=True)
        status_grid.columnconfigure(1, weight=1)

        ttk.Label(status_grid, text="Dose:").grid(row=0, column=0, sticky=tk.W, padx=(0, 8), pady=2)
        self.__status_dose_value = ttk.Label(status_grid, text="N/A")
        self.__status_dose_value.grid(row=0, column=1, sticky=tk.W, pady=2)

        ttk.Label(status_grid, text="Time:").grid(row=1, column=0, sticky=tk.W, padx=(0, 8), pady=2)
        self.__status_time_value = ttk.Label(status_grid, text="N/A")
        self.__status_time_value.grid(row=1, column=1, sticky=tk.W, pady=2)

        ttk.Label(status_grid, text="Laser:").grid(row=2, column=0, sticky=tk.W, padx=(0, 8), pady=2)
        self.__status_laser_value = ttk.Label(status_grid, text="Unknown")
        self.__status_laser_value.grid(row=2, column=1, sticky=tk.W, pady=2)

        ttk.Label(status_grid, text="Chopper:").grid(row=3, column=0, sticky=tk.W, padx=(0, 8), pady=2)
        self.__status_chopper_value = ttk.Label(status_grid, text="Unknown")
        self.__status_chopper_value.grid(row=3, column=1, sticky=tk.W, pady=2)

        ttk.Label(status_grid, text="Chopper Phase:").grid(row=4, column=0, sticky=tk.W, padx=(0, 8), pady=2)
        self.__status_chopper_phase_value = ttk.Label(status_grid, text="Phase: current=N/A target=N/A")
        self.__status_chopper_phase_value.grid(row=4, column=1, sticky=tk.W, pady=2)

        ttk.Label(status_grid, text="Tin Target:").grid(row=5, column=0, sticky=tk.W, padx=(0, 8), pady=2)
        self.__status_target_value = ttk.Label(status_grid, text="Unknown")
        self.__status_target_value.grid(row=5, column=1, sticky=tk.W, pady=2)

        ttk.Label(status_grid, text="Target Time Position:").grid(row=6, column=0, sticky=tk.W, padx=(0, 8), pady=2)
        self.__status_target_time_value = ttk.Label(status_grid, text="Time: t=N/A seg=N/A")
        self.__status_target_time_value.grid(row=6, column=1, sticky=tk.W, pady=2)

        self.__laser_status_canvas = tk.Canvas(status_container, width=240, height=42, bg="black", highlightthickness=0)
        self.__laser_status_canvas.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=(0, 10))

        # Settings frame with scrolling capability (using a simple Frame)
        self.__settings_frame = ttk.Frame(settings_container, padding=10)
        self.__settings_frame.pack(fill=tk.BOTH, expand=True)
        settings_frame = self.__settings_frame

        # Row counter for grid layout
        row = 0

        # --- Basic Info ---
        basic_label = ttk.Label(settings_frame, text="Basic Info", font=("TkDefaultFont", 10, "bold"))
        basic_label.grid(row=row, column=0, columnspan=2, sticky=tk.W, pady=(0, 5))
        row += 1

        ttk.Label(settings_frame, text="Name:").grid(row=row, column=0, sticky=tk.W, pady=2)
        self.__name_input = ttk.Entry(settings_frame, width=28)
        self.__name_input.grid(row=row, column=1, sticky=tk.EW, pady=2)
        row += 1

        ttk.Label(settings_frame, text="Description:").grid(row=row, column=0, sticky=tk.NW, pady=2)
        self.__description_input = tk.Text(settings_frame, height=3, width=28, wrap=tk.WORD)
        self.__description_input.grid(row=row, column=1, sticky=tk.EW, pady=2)
        row += 1

        row += 1  # Spacing

        # --- Target Settings ---
        target_label = ttk.Label(settings_frame, text="Target Settings", font=("TkDefaultFont", 10, "bold"))
        target_label.grid(row=row, column=0, columnspan=2, sticky=tk.W, pady=(0, 5))
        row += 1

        # Radio buttons for target type
        self.__time_radio = ttk.Radiobutton(settings_frame, text="Target Time", variable=self.__target_type_var, value="time", command=self.__update_target_unit_label)
        self.__time_radio.grid(row=row, column=0, sticky=tk.W, pady=2)
        
        self.__dose_radio = ttk.Radiobutton(settings_frame, text="Target Dose", variable=self.__target_type_var, value="dose", command=self.__update_target_unit_label)
        self.__dose_radio.grid(row=row, column=1, sticky=tk.W, pady=2)
        row += 1

        # Target value input with dynamic unit label
        ttk.Label(settings_frame, text="Target Value:").grid(row=row, column=0, sticky=tk.W, pady=2)
        input_frame = ttk.Frame(settings_frame)
        input_frame.grid(row=row, column=1, sticky=tk.EW, pady=2)
        
        self.__target_input = ttk.Entry(input_frame, width=10)
        self.__target_input.pack(side=tk.LEFT, padx=(0, 5))

        self.__target_type_label = ttk.Label(input_frame, text="seconds")
        self.__target_type_label.pack(side=tk.LEFT)
        row += 1

        row += 1  # Spacing

        # --- Sample Settings ---
        sample_label = ttk.Label(settings_frame, text="Sample Settings", font=("TkDefaultFont", 10, "bold"))
        sample_label.grid(row=row, column=0, columnspan=2, sticky=tk.W, pady=(0, 5))
        row += 1

        # Operator dropdown
        ttk.Label(settings_frame, text="Operator:").grid(row=row, column=0, sticky=tk.W, pady=2)
        self.__operator_combo = ttk.Combobox(settings_frame, values=self.__operator_options, state="readonly", width=20)
        self.__operator_combo.grid(row=row, column=1, sticky=tk.EW, pady=2)
        row += 1

        # Sample dropdown
        ttk.Label(settings_frame, text="Sample:").grid(row=row, column=0, sticky=tk.W, pady=2)
        self.__sample_combo = ttk.Combobox(settings_frame, values=self.__sample_options, state="readonly", width=20)
        self.__sample_combo.grid(row=row, column=1, sticky=tk.EW, pady=2)
        row += 1

        # Sample type dropdown
        ttk.Label(settings_frame, text="Sample Type:").grid(row=row, column=0, sticky=tk.W, pady=2)
        self.__sample_type_combo = ttk.Combobox(settings_frame, values=self.__sample_type_options, state="readonly", width=20)
        self.__sample_type_combo.grid(row=row, column=1, sticky=tk.EW, pady=2)
        row += 1

        # Zr filter dropdown
        ttk.Label(settings_frame, text="Zr Filter:").grid(row=row, column=0, sticky=tk.W, pady=2)
        self.__zr_filter_combo = ttk.Combobox(settings_frame, values=self.__zr_filter_options, state="readonly", width=20)
        self.__zr_filter_combo.grid(row=row, column=1, sticky=tk.EW, pady=2)
        row += 1

        row += 1  # Spacing

        # --- Pressure/Flow Settings ---
        pressure_label = ttk.Label(settings_frame, text="Pressure & Flow", font=("TkDefaultFont", 10, "bold"))
        pressure_label.grid(row=row, column=0, columnspan=2, sticky=tk.W, pady=(0, 5))
        row += 1

        # Base pressure
        ttk.Label(settings_frame, text="Base Pressure:").grid(row=row, column=0, sticky=tk.W, pady=2)
        pressure_frame = ttk.Frame(settings_frame)
        pressure_frame.grid(row=row, column=1, sticky=tk.EW, pady=2)
        
        self.__base_pressure_input = ttk.Entry(pressure_frame, width=10)
        self.__base_pressure_input.pack(side=tk.LEFT, padx=(0, 5))
        
        ttk.Label(pressure_frame, text="Torr").pack(side=tk.LEFT)
        row += 1

        # Operating pressure
        ttk.Label(settings_frame, text="Operating Pressure:").grid(row=row, column=0, sticky=tk.W, pady=2)
        op_pressure_frame = ttk.Frame(settings_frame)
        op_pressure_frame.grid(row=row, column=1, sticky=tk.EW, pady=2)
        
        self.__operating_pressure_input = ttk.Entry(op_pressure_frame, width=10)
        self.__operating_pressure_input.pack(side=tk.LEFT, padx=(0, 5))
        
        ttk.Label(op_pressure_frame, text="Torr").pack(side=tk.LEFT)
        row += 1

        # Flowrate
        ttk.Label(settings_frame, text="Flowrate:").grid(row=row, column=0, sticky=tk.W, pady=2)
        flowrate_frame = ttk.Frame(settings_frame)
        flowrate_frame.grid(row=row, column=1, sticky=tk.EW, pady=2)
        
        self.__flowrate_input = ttk.Entry(flowrate_frame, width=10)
        self.__flowrate_input.pack(side=tk.LEFT, padx=(0, 5))
        
        ttk.Label(flowrate_frame, text="SCCM").pack(side=tk.LEFT)
        row += 1

        row += 1  # Spacing

        # --- Control Buttons ---
        button_frame = ttk.Frame(settings_frame)
        button_frame.grid(row=row, column=0, columnspan=2, sticky=tk.EW, pady=(10, 0))

        self.__refresh_button = ttk.Button(button_frame, text="Refresh Values", command=self.__refresh_options)
        self.__refresh_button.pack(side=tk.LEFT, padx=(0, 5))

        self.__apply_button = ttk.Button(button_frame, text="Apply Settings", command=self.__on_apply_settings)
        self.__apply_button.pack(side=tk.LEFT)

        # Configure grid weights for proper expansion
        settings_frame.columnconfigure(1, weight=1)

    def close(self):
        self.__exp_ctl.on_close()
        self.__exp_itf.close()
        self.__client.close()
        self.__logger_sock.close()

    def redraw_gui(self):
        VIS_SCALE = 2
        center_x = self.__canvas.winfo_width() // 2
        center_y = self.__canvas.winfo_height() // 2

        try:
            self.__selected_sample = int(self.__sample_combo.get())
        except (ValueError, tk.TclError):
            self.__selected_sample = None

        self.__canvas.delete("all")

        val = self.__position_kv.value if self.__position_kv is not None else (0, 0)
        if val is not None:
            rp, lp = val
        else:
            lp, rp = (0, 0)
        rp *= 180 / math.pi
        #lp, rp = (0, 0)
        #if self.__position_kv is not None:
        #    print(f"Position KV value: {self.__position_kv.value}")

        exposure_x = center_x + stage_client.EXPOSURE_OFFSET_Z * VIS_SCALE
        exposure_y = center_y - stage_client.EXPOSURE_OFFSET_X * VIS_SCALE

        draw_sample_stage(
            self.__canvas,
            center_x=center_x,
            center_y=center_y,
            vis_scale=VIS_SCALE,
            lin_pos=lp,
            current_angle=rp,
            samples=self.__samples,
            ring_radii=RING_RADII,
            exposure_x=exposure_x,
            exposure_y=exposure_y,
            platform_color="lightblue",
            highlight_sample=self.__selected_sample
        )

        self.__canvas.create_text(center_x - 150, center_y, text="\n".join("DOOR"), fill="red", angle=0, font=("Helvetica", 25, "bold"))

        self.__root.after(50, self.redraw_gui)

    def sync_settings(self):
        self.__sync_settings_from_run_state()

        self.__root.after(500, self.sync_settings)
    


UUID_EXPOSURE_CONTROLLER = uuid.uuid3(uuid.NAMESPACE_OID, "Exposure Controller")
if __name__ == "__main__":
    root = tk.Tk()
    app = ExposureControllerGUI(root)
    root.mainloop()

    app.close()