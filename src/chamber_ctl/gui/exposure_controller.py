import os
import json
import tkinter as tk
from tkinter import ttk
from tkinter import messagebox
import math
import pickle
import uuid
import segment_bytes
from collections.abc import Callable, Iterable

from ipi_ecs.core.tcp import TCPClientSocket
from ipi_ecs.dds import client, subsystem, types
from ipi_ecs.logging.client import LogClient
from ipi_ecs.gui.experiment_controller_gui import ExperimentInterface, ExperimentControllerGUI
from ipi_ecs.cli.captive_cli import wait_for

from chamber_ctl import ECS_IP
from chamber_ctl.data.calibration import (
    CalibrationRepository,
    SourceCalibrationBinding,
    SourceKey,
    source_configuration_run_tags,
)
from chamber_ctl.gui.acquisition import acquisition_dose_rate_metric, acquisition_status_metrics
from chamber_ctl.gui.sample_motion_gui import draw_sample_stage, build_sample_data, RING_RADII
from chamber_ctl.gui.source_calibration_editor import (
    calibration_option,
    edit_source_calibrations,
    source_calibration_summary,
)
from chamber_ctl.subsystems import uuids
from chamber_ctl.subsystems.exposure_controller import ExposureSettings
from chamber_ctl.subsystems.laser import LaserSyncStatus
from chamber_ctl.subsystems.siglent_observer import observer_subsystem_uuid
from chamber_ctl.subsystems.settings_presets import SettingsPresets
from euv_acquisition.health import AcquisitionHealth
from euv_acquisition.source_identity import RED_PITAYA_SOURCE_ID, RED_PITAYA_SOURCE_KIND
import chamber_ctl.subsystems.sample_motion as stage_client


class ExposureControllerGUI():
    LIVE_STATUS_UPDATE_MS = 100

    def __init__(
        self,
        root,
        own_window: bool = True,
        source_options_provider: Callable[[], Iterable[SourceKey]] | None = None,
    ):
        self.__own_window = own_window
        self.__exp_itf = ExperimentInterface("exposure", UUID_EXPOSURE_CONTROLLER, exp_settings_type=ExposureSettings)
        self.__exp_ctl = ExperimentControllerGUI(root, self.__exp_itf, own_window=own_window)
        self.__root = root
        self.__source_options_provider = source_options_provider

        self.__samples = build_sample_data()

        self.__sample_options = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]
        self.__zr_filter_options = []
        self.__sample_type_options = []
        self.__operator_options = []
        
        self.__data_path = os.path.join(os.environ["EUVL_PATH"], "datasets")
        self.__presets = SettingsPresets(self.__data_path)
        self.__source_calibration_options = {}
        self.__source_calibrations: tuple[SourceCalibrationBinding, ...] = ()
        self.__primary_source: SourceKey | None = None
        self.__source_calibration_text = tk.StringVar(value="None")

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
        self.__source_calibration_button = None
        self.__base_pressure_input = None
        self.__operating_pressure_input = None
        self.__flowrate_input = None
        self.__chopper_frequency_input = None
        self.__settings_frame = None
        self.__time_radio = None
        self.__dose_radio = None
        self.__refresh_button = None
        self.__apply_button = None
        self.__settings_locked = False
        self.__settings_lock_reason = ""
        self.__automation_lock_text = tk.StringVar(value="Settings are editable.")

        c_uuid = uuid.uuid4()
        s_uuid = uuid.uuid4()

        self.__logger_sock = TCPClientSocket()

        self.__logger_sock.connect(("127.0.0.1", 11751))
        self.__logger_sock.start()

        self.__logger = LogClient(self.__logger_sock, origin_uuid=c_uuid)

        self.__did_config = False
        self.__subsystem = None
        self.__observer_live_kvs: dict[SourceKey, tuple[object, object]] = {}
        self.__observer_live_labels: dict[SourceKey, tuple[object, object, object]] = {}

        self.__position_kv = None

        self.__dose_kv = None
        self.__time_kv = None
        self.__acquisition_status_kv = None
        self.__acquisition_health_kv = None
        self.__laser_status_kv = None
        self.__target_status_kv = None

        self.__status_dose_value = None
        self.__status_time_value = None
        self.__live_dose_text = tk.StringVar(value="N/A")
        self.__live_dose_rate_text = tk.StringVar(value="N/A")
        self.__live_dose_rate_window_text = tk.StringVar(value="500 ms rolling")
        self.__live_runtime_text = tk.StringVar(value="Transmitting time: N/A")
        self.__status_control_source_value = None
        self.__live_sources_frame = None
        self.__status_acquisition_value = None
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
            repository = CalibrationRepository(self.__data_path)
            try:
                source_profiles = repository.list_all()
            finally:
                repository.close()
            self.__source_calibration_options = {
                f"{profile.name} r{profile.revision} | {profile.profile_id}": (str(profile.profile_id), profile.revision)
                for profile in source_profiles
            }
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

    def __edit_source_calibrations(self):
        if self.__settings_locked:
            return
        selected = edit_source_calibrations(
            self.__root,
            self.__source_calibrations,
            self.__source_calibration_options,
            self.__primary_source,
            data_path=self.__data_path,
            source_options=self.__available_sources(),
            on_calibration_created=self.__on_calibration_created,
        )
        if selected is None:
            return
        self.__source_calibrations = selected.calibrations
        self.__primary_source = selected.primary_source
        self.__source_calibration_text.set(
            source_calibration_summary(selected.calibrations, selected.primary_source)
        )
        self.__refresh_observer_live_sources()
        self.__exp_itf.set_run_tags(
            source_configuration_run_tags(selected.calibrations, selected.primary_source)
        )

    def __available_sources(self) -> tuple[SourceKey, ...]:
        sources = {SourceKey(RED_PITAYA_SOURCE_KIND, RED_PITAYA_SOURCE_ID)}
        status = self.__get_acquisition_status() or {}
        source_kind = status.get("configured_source_kind")
        source_id = status.get("configured_source_id")
        if isinstance(source_kind, str) and isinstance(source_id, str):
            try:
                sources.add(SourceKey(source_kind, source_id))
            except ValueError:
                pass
        if self.__source_options_provider is not None:
            sources.update(self.__source_options_provider())
        return tuple(sorted(sources))

    def __on_calibration_created(self, profile) -> None:
        label, value = calibration_option(profile)
        self.__source_calibration_options[label] = value

    def __update_target_unit_label(self, *args):
        """Update the unit label based on target type selection."""
        if self.__target_type_label is not None:
            target_type = self.__target_type_var.get()
            if target_type == "time":
                self.__target_type_label.config(text="seconds")
            else:
                self.__target_type_label.config(text="mj/cm2")

    def __on_got_subsystem(self, handle: client._RegisteredSubsystemHandle):
        self.__subsystem = handle
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
            uuids.UUID_EUV_ACQUISITION_CONTROLLER,
            subsystem.KVDescriptor(types.FloatTypeSpecifier(), b"cur_dose", True, True, False)
        )

        self.__time_kv = handle.add_remote_kv(
            uuids.UUID_EUV_ACQUISITION_CONTROLLER,
            subsystem.KVDescriptor(types.FloatTypeSpecifier(), b"cur_time", True, True, False)
        )

        self.__acquisition_status_kv = handle.add_remote_kv(
            uuids.UUID_EUV_ACQUISITION_CONTROLLER,
            subsystem.KVDescriptor(types.ByteTypeSpecifier(), b"acquisition_status", True, True, False),
        )
        self.__acquisition_health_kv = handle.add_remote_kv(
            uuids.UUID_EUV_ACQUISITION_CONTROLLER,
            subsystem.KVDescriptor(types.ByteTypeSpecifier(), b"acquisition_health", True, True, False),
        )

        self.__laser_status_kv = handle.add_remote_kv(
            uuids.UUID_LASER_CONTROLLER,
            subsystem.KVDescriptor(types.ByteTypeSpecifier(), b"status", True, True, False),
        )

        self.__target_status_kv = handle.add_remote_kv(
            uuids.UUID_TARGET_CONTROLLER,
            subsystem.KVDescriptor(types.VectorTypeSpecifier(types.ByteTypeSpecifier(), 2), b"status", True, True, False),
        )

        self.__refresh_observer_live_sources()
        self.__refresh_queue_from_remote(update_status=False)

    def __live_source_keys(self) -> tuple[SourceKey, ...]:
        sources = {binding.source_key for binding in self.__source_calibrations}
        if self.__source_options_provider is not None:
            try:
                sources.update(self.__source_options_provider())
            except Exception:
                pass
        status = self.__get_acquisition_status() or {}
        source_kind = status.get("configured_source_kind") or status.get("source_kind")
        source_id = status.get("configured_source_id") or status.get("source_id")
        if isinstance(source_kind, str) and isinstance(source_id, str):
            try:
                sources.add(SourceKey(source_kind, source_id))
            except ValueError:
                pass
        if not sources:
            sources.add(SourceKey(RED_PITAYA_SOURCE_KIND, RED_PITAYA_SOURCE_ID))
        return tuple(sorted(sources))

    def __control_source_key(self) -> SourceKey:
        status = self.__get_acquisition_status() or {}
        try:
            return SourceKey(
                str(status.get("configured_source_kind") or status.get("source_kind") or RED_PITAYA_SOURCE_KIND),
                str(status.get("configured_source_id") or status.get("source_id") or RED_PITAYA_SOURCE_ID),
            )
        except ValueError:
            return SourceKey(RED_PITAYA_SOURCE_KIND, RED_PITAYA_SOURCE_ID)

    def __refresh_observer_live_sources(self) -> None:
        handle = getattr(self, "_ExposureControllerGUI__subsystem", None)
        if handle is None:
            return
        control_source = self.__control_source_key()
        for source in self.__live_source_keys():
            if source == control_source or source in self.__observer_live_kvs:
                continue
            source_uuid = observer_subsystem_uuid(source)
            dose = handle.add_remote_kv(
                source_uuid,
                subsystem.KVDescriptor(types.FloatTypeSpecifier(), b"cur_dose", True, True, False),
            )
            runtime = handle.add_remote_kv(
                source_uuid,
                subsystem.KVDescriptor(types.FloatTypeSpecifier(), b"cur_time", True, True, False),
            )
            self.__observer_live_kvs[source] = (dose, runtime)

    @staticmethod
    def __format_live_value(value, unit: str) -> str:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return "N/A"
        if not math.isfinite(numeric):
            return "N/A"
        return f"{numeric:.2f} {unit}"

    def __update_live_source_rows(self, control_dose, control_time) -> None:
        self.__status_dose_value.config(text=self.__format_live_value(control_dose, "mJ/cm²"))
        self.__status_time_value.config(text=self.__format_live_value(control_time, "s"))
        frame = self.__live_sources_frame
        if frame is None:
            return
        control_source = self.__control_source_key()
        control_roles = ["control"]
        if control_source == self.__primary_source:
            control_roles.append("default")
        self.__status_control_source_value.config(
            text=f"{control_source.source_kind}/{control_source.source_id} ({', '.join(control_roles)})"
        )

        observer_sources = [source for source in self.__live_source_keys() if source != control_source]
        active_sources = set(observer_sources)
        for source, labels in tuple(self.__observer_live_labels.items()):
            if source not in active_sources:
                for label in labels:
                    label.grid_remove()
        for row, source in enumerate(observer_sources, start=2):
            labels = self.__observer_live_labels.get(source)
            if labels is None:
                labels = (
                    ttk.Label(frame, wraplength=170),
                    ttk.Label(frame, width=14),
                    ttk.Label(frame, width=9),
                )
                self.__observer_live_labels[source] = labels
            roles = ["default"] if source == self.__primary_source else []
            source_text = f"{source.source_kind}/{source.source_id}"
            if roles:
                source_text += f" ({', '.join(roles)})"
            dose_kv, time_kv = self.__observer_live_kvs.get(source, (None, None))
            dose_value = None if dose_kv is None else dose_kv.value
            time_value = None if time_kv is None else time_kv.value
            values = (
                source_text,
                self.__format_live_value(dose_value, "mJ/cm²"),
                self.__format_live_value(time_value, "s"),
            )
            for column, (label, text) in enumerate(zip(labels, values)):
                label.config(text=text)
                label.grid(row=row, column=column, sticky=tk.W, padx=(0, 8), pady=1)

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
        acquisition_status = self.__get_acquisition_status()
        dose_value = self.__dose_kv.value if self.__dose_kv is not None else None
        if dose_value is None and acquisition_status is not None:
            dose_value = acquisition_status.get("accumulated_dose_mj_cm2")
        time_value = self.__time_kv.value if self.__time_kv is not None else None
        if time_value is None and acquisition_status is not None:
            time_value = acquisition_status.get("transmitting_runtime_seconds")
        self.__refresh_observer_live_sources()
        self.__update_live_source_rows(dose_value, time_value)
        live_status = dict(acquisition_status or {})
        if dose_value is not None:
            live_status["accumulated_dose_mj_cm2"] = dose_value
        if time_value is not None:
            live_status["transmitting_runtime_seconds"] = time_value
        dose, runtime = acquisition_status_metrics(live_status)
        dose_rate, dose_rate_window = acquisition_dose_rate_metric(live_status)
        self.__live_dose_text.set(dose)
        self.__live_dose_rate_text.set(dose_rate)
        self.__live_dose_rate_window_text.set(dose_rate_window)
        self.__live_runtime_text.set(f"Transmitting time: {runtime}")

        if self.__status_acquisition_value is not None:
            self.__status_acquisition_value.config(text=self.__format_acquisition_status())

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
            self.__root.after(self.LIVE_STATUS_UPDATE_MS, self.__update_live_status)

    def __get_acquisition_status(self) -> dict | None:
        status_value = self.__acquisition_status_kv.value if self.__acquisition_status_kv is not None else None
        try:
            status_payload = bytes(status_value) if isinstance(status_value, list) else status_value
            status = json.loads(bytes(status_payload).decode("utf-8")) if status_payload is not None else None
            return status if isinstance(status, dict) else None
        except Exception:
            return None

    def __format_acquisition_status(self) -> str:
        status = self.__get_acquisition_status()
        health_value = self.__acquisition_health_kv.value if self.__acquisition_health_kv is not None else None
        try:
            health_payload = bytes(health_value) if isinstance(health_value, list) else health_value
            health = AcquisitionHealth.decode(health_payload) if health_payload is not None else None
        except Exception:
            return "Unavailable"
        if status is None:
            return "Unavailable"
        state = str(status.get("state", "unknown"))
        if health is None:
            return state
        if state == "recovery_required":
            detail = status.get("finalization_detail") or "Digitizer artifact recovery is required."
            return f"Recovery required: {detail}"
        details = []
        source_kind = status.get("source_kind")
        source_id = status.get("source_id")
        if source_kind:
            details.append(str(source_kind) if not source_id else f"{source_kind}:{source_id}")
        if health.last_sequence is not None:
            details.append(f"seq {health.last_sequence}")
        clipped_count = status.get("clipped_pulse_count", 0)
        if clipped_count:
            details.append(f"clipped {clipped_count}")
        if state == "finalizing" and status.get("finalization_phase"):
            details.append(str(status["finalization_phase"]))
        prefix = f"{state}, " + ", ".join(details) if details else state
        if health.pulse_loss:
            return f"{prefix}: pulse loss"
        if health.recovery_ready and not health.resume_authorized:
            return f"{prefix}: recovery ready"
        if health.resume_authorized:
            return f"{prefix}: recovery authorized"
        if health.last_pulse_age_seconds is None:
            return prefix
        detail = status.get("finalization_detail")
        suffix = f", {detail}" if state == "finalizing" and detail else ""
        return f"{prefix}, pulse {health.last_pulse_age_seconds:.2f}s ago{suffix}"

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
        settings["chopper_frequency_hz"] = self.__chopper_frequency_input.get()

        settings["calibration_profile_id"] = ""
        settings["calibration_revision"] = "0"

        return settings

    def __on_apply_settings(self):
        """Apply current settings to the experiment controller."""
        self.__sync_settings_from_run_state()
        if self.__settings_locked:
            self.__automation_lock_text.set(self.__settings_lock_reason)
            return
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

    def __migrate_legacy_calibration(self, profile_id, revision) -> None:
        if self.__source_calibrations:
            return
        try:
            normalized_profile_id = uuid.UUID(str(profile_id))
            revision = int(revision)
        except (TypeError, ValueError, AttributeError):
            return
        if revision < 1:
            return
        status = self.__get_acquisition_status() or {}
        source_key = SourceKey(
            str(status.get("configured_source_kind") or RED_PITAYA_SOURCE_KIND),
            str(status.get("configured_source_id") or RED_PITAYA_SOURCE_ID),
        )
        binding = SourceCalibrationBinding(
            source_key.source_kind,
            source_key.source_id,
            normalized_profile_id,
            revision,
        )
        self.__source_calibrations = (binding,)
        self.__primary_source = source_key
        self.__source_calibration_text.set(
            source_calibration_summary(self.__source_calibrations, self.__primary_source)
        )
        self.__exp_itf.set_run_tags(
            source_configuration_run_tags(self.__source_calibrations, self.__primary_source)
        )

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
        self.__set_entry_text(self.__chopper_frequency_input, settings.get("chopper_frequency_hz", ""))
        self.__migrate_legacy_calibration(
            settings.get("calibration_profile_id"),
            settings.get("calibration_revision"),
        )

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
            self.__chopper_frequency_input,
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
        if self.__source_calibration_button is not None:
            self.__source_calibration_button.config(state=button_state)

    def __sync_settings_from_run_state(self):
        has_experiment = self.__exp_itf.get_experiment() is not None
        if has_experiment:
            self.__sync_settings_from_experiment()

        automation_owner = self.__exp_itf.get_automation_owner()
        should_lock = has_experiment or automation_owner is not None
        if has_experiment:
            reason = "Settings locked while an exposure is active."
        elif automation_owner is not None:
            reason = f"Settings locked: automation held by {automation_owner}."
        else:
            reason = "Settings are editable."
        if should_lock != self.__settings_locked or reason != self.__settings_lock_reason:
            self.__settings_locked = should_lock
            self.__settings_lock_reason = reason
            self.__automation_lock_text.set(reason)
            self.__set_settings_controls_enabled(not should_lock)

    def initialize_component(self):
        live_metrics = ttk.Frame(self.__root, padding=(18, 8))
        live_metrics.pack(fill=tk.X, padx=10, pady=(0, 2))
        live_metrics.columnconfigure(0, weight=1, uniform="exposure-live-dose")
        live_metrics.columnconfigure(2, weight=1, uniform="exposure-live-dose")

        dose_metric = ttk.Frame(live_metrics)
        dose_metric.grid(row=0, column=0, sticky=tk.EW, padx=(8, 24))
        ttk.Label(dose_metric, text="LIVE DOSE", font=("TkDefaultFont", 9, "bold")).pack(anchor=tk.W)
        ttk.Label(dose_metric, textvariable=self.__live_dose_text, font=("TkDefaultFont", 30, "bold")).pack(anchor=tk.W)
        ttk.Label(dose_metric, textvariable=self.__live_runtime_text).pack(anchor=tk.W, pady=(2, 0))

        ttk.Separator(live_metrics, orient=tk.VERTICAL).grid(row=0, column=1, sticky=tk.NS)

        rate_metric = ttk.Frame(live_metrics)
        rate_metric.grid(row=0, column=2, sticky=tk.EW, padx=(24, 8))
        ttk.Label(rate_metric, text="LIVE DOSE RATE", font=("TkDefaultFont", 9, "bold")).pack(anchor=tk.W)
        ttk.Label(rate_metric, textvariable=self.__live_dose_rate_text, font=("TkDefaultFont", 30, "bold")).pack(anchor=tk.W)
        ttk.Label(rate_metric, textvariable=self.__live_dose_rate_window_text).pack(anchor=tk.W, pady=(2, 0))

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

        ttk.Label(status_grid, text="Live Sources", font=("TkDefaultFont", 10, "bold")).grid(
            row=0,
            column=0,
            columnspan=2,
            sticky=tk.W,
            pady=(0, 4),
        )
        self.__live_sources_frame = ttk.Frame(status_grid)
        self.__live_sources_frame.grid(row=1, column=0, columnspan=2, sticky=tk.EW, pady=(0, 6))
        ttk.Label(self.__live_sources_frame, text="Source", font=("TkDefaultFont", 9, "bold")).grid(row=0, column=0, sticky=tk.W, padx=(0, 8))
        ttk.Label(self.__live_sources_frame, text="Dose", font=("TkDefaultFont", 9, "bold")).grid(row=0, column=1, sticky=tk.W, padx=(0, 8))
        ttk.Label(self.__live_sources_frame, text="Time", font=("TkDefaultFont", 9, "bold")).grid(row=0, column=2, sticky=tk.W)
        self.__status_control_source_value = ttk.Label(self.__live_sources_frame, wraplength=170)
        self.__status_control_source_value.grid(row=1, column=0, sticky=tk.W, padx=(0, 8), pady=1)
        self.__status_dose_value = ttk.Label(self.__live_sources_frame, text="N/A", width=14)
        self.__status_dose_value.grid(row=1, column=1, sticky=tk.W, padx=(0, 8), pady=1)
        self.__status_time_value = ttk.Label(self.__live_sources_frame, text="N/A", width=9)
        self.__status_time_value.grid(row=1, column=2, sticky=tk.W, pady=1)

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

        ttk.Label(status_grid, text="Acquisition:").grid(row=7, column=0, sticky=tk.NW, padx=(0, 8), pady=2)
        self.__status_acquisition_value = ttk.Label(status_grid, text="Unavailable", wraplength=180)
        self.__status_acquisition_value.grid(row=7, column=1, sticky=tk.W, pady=2)

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

        ttk.Label(settings_frame, text="Acquisition Sources:").grid(row=row, column=0, sticky=tk.NW, pady=2)
        source_calibration_frame = ttk.Frame(settings_frame)
        source_calibration_frame.grid(row=row, column=1, sticky=tk.EW, pady=2)
        source_calibration_frame.columnconfigure(0, weight=1)
        self.__source_calibration_button = ttk.Button(
            source_calibration_frame,
            text="Configure...",
            command=self.__edit_source_calibrations,
        )
        self.__source_calibration_button.grid(row=0, column=0, sticky=tk.W)
        ttk.Label(
            source_calibration_frame,
            textvariable=self.__source_calibration_text,
            wraplength=260,
        ).grid(row=1, column=0, sticky=tk.W, pady=(2, 0))
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

        ttk.Label(settings_frame, text="Chopper Frequency:").grid(row=row, column=0, sticky=tk.W, pady=2)
        frequency_frame = ttk.Frame(settings_frame)
        frequency_frame.grid(row=row, column=1, sticky=tk.EW, pady=2)
        self.__chopper_frequency_input = ttk.Entry(frequency_frame, width=10)
        self.__chopper_frequency_input.insert(0, "192")
        self.__chopper_frequency_input.pack(side=tk.LEFT, padx=(0, 5))
        ttk.Label(frequency_frame, text="Hz").pack(side=tk.LEFT)
        row += 1

        row += 1  # Spacing

        # --- Control Buttons ---
        button_frame = ttk.Frame(settings_frame)
        button_frame.grid(row=row, column=0, columnspan=2, sticky=tk.EW, pady=(10, 0))

        self.__refresh_button = ttk.Button(button_frame, text="Refresh Values", command=self.__refresh_options)
        self.__refresh_button.pack(side=tk.LEFT, padx=(0, 5))

        self.__apply_button = ttk.Button(button_frame, text="Apply Settings", command=self.__on_apply_settings)
        self.__apply_button.pack(side=tk.LEFT)

        ttk.Label(settings_frame, textvariable=self.__automation_lock_text, foreground="#9a4d35", wraplength=320).grid(
            row=row + 1,
            column=0,
            columnspan=2,
            sticky=tk.W,
            pady=(6, 0),
        )

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