import argparse
import pickle
import queue
import struct
import sys
import time
import tkinter as tk
import traceback
import uuid

import segment_bytes

from ipi_ecs.core import daemon
import ipi_ecs.core.tcp as tcp
import ipi_ecs.dds.client as client
import ipi_ecs.dds.magics as magics
import ipi_ecs.dds.subsystem as subsystem
import ipi_ecs.dds.types as types
from ipi_ecs.logging.client import LogClient
from ipi_ecs.cli.captive_cli import wait_for

from chamber_ctl import ECS_IP
from chamber_ctl.subsystems import uuids
from chamber_ctl.subsystems.laser import LaserSyncStatus


class LaserSyncTestGUI:
    def __init__(self, root, own_window: bool = True):
        self.__root = root
        self.__own_window = own_window
        self.__dialog_parent = root if own_window else root.winfo_toplevel()
        self.__run = True
        self.__connected = False

        self.__client = None
        self.__subsystem = None

        self.__logger_sock = None
        self.__logger = None

        self.__preinit_phase_kv = None
        self.__target_phase_kv = None
        self.__initial_phase_kv = None
        self.__skew_rate_kv = None
        self.__laser_warmup_time_kv = None
        self.__chopper_startup_time_kv = None
        self.__chopper_frequency_kv = None
        self.__status_kv = None
        self.__exp_state_kv = None

        self.__preinit_event = None
        self.__init_event = None
        self.__stop_event = None
        self.__laser_on_event = None
        self.__laser_off_event = None
        self.__chopper_on_event = None
        self.__chopper_off_event = None
        self.__set_phase_event = None

        self.__do_timed_exposure_event = None
        self.__do_continuous_exposure_event = None
        self.__laser_shut_event = None

        self.__current_status = None
        self.__ui_queue = queue.Queue()
        self.__cmd_queue = queue.Queue()

        self.__preinit_phase_value = 0.0
        self.__target_phase_value = 0.0
        self.__initial_phase_value = 0.0
        self.__skew_rate_value = 1.0
        self.__laser_warmup_time_value = 5.0
        self.__chopper_startup_time_value = 5.0
        self.__chopper_frequency_value = 192.0

        self.__progress_dialog = None
        self.__progress_text = None

        self.__build_ui(root)
        self.__setup_client()

        self.__daemon = daemon.Daemon(exception_handler=self.handle_exception)
        self.__daemon.add(self.__worker_thread)
        self.__daemon.start()

        self.__root.after(100, self.__ui_tick)
        self.__root.after(800, self.__periodic_refresh)

    def handle_exception(self, e: Exception):
        self.__log("Caught exception on daemon thread!", level="ERROR")
        for line in traceback.format_exception(None, e, e.__traceback__):
            for split in line.split('\n'):
                self.__log(split, level="ERROR")
    
    def __log(self, msg, level = "INFO", **data):
        if self.__logger is None:
            print(level, msg)
            return
        
        self.__logger.log(msg, level=level, l_type="SW", subsystem="Laser Sync GUI", **data)

    def __build_ui(self, root):
        if self.__own_window and hasattr(root, "title"):
            root.title("Laser Sync Control")
        if self.__own_window and hasattr(root, "geometry"):
            root.geometry("900x600")

        frame = tk.Frame(root)
        frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)

        title = tk.Label(frame, text="Laser Sync Control", font=("Arial", 24, "bold"))
        title.pack(anchor="w")

        self.__connection_label = tk.Label(frame, text="Connection: DISCONNECTED", font=("Arial", 12))
        self.__connection_label.pack(anchor="w", pady=(8, 0))

        self.__exp_state_label = tk.Label(frame, text="Experiment State: Unknown", font=("Arial", 12))
        self.__exp_state_label.pack(anchor="w")

        phase_frame = tk.LabelFrame(frame, text="Phase Setup")
        phase_frame.pack(fill=tk.X, pady=(12, 8))

        tk.Label(phase_frame, text="Preinit Phase", font=("Arial", 11)).grid(row=0, column=0, padx=8, pady=8, sticky="w")
        self.__preinit_phase_entry = tk.Entry(phase_frame, width=16)
        self.__preinit_phase_entry.insert(0, "0.0")
        self.__preinit_phase_entry.grid(row=0, column=1, padx=8, pady=8, sticky="w")

        tk.Button(phase_frame, text="Set Preinit Phase", command=self.__on_set_preinit_phase).grid(row=0, column=2, padx=8, pady=8)

        tk.Label(phase_frame, text="Target Phase", font=("Arial", 11)).grid(row=1, column=0, padx=8, pady=8, sticky="w")
        self.__target_phase_entry = tk.Entry(phase_frame, width=16)
        self.__target_phase_entry.insert(0, "0.0")
        self.__target_phase_entry.grid(row=1, column=1, padx=8, pady=8, sticky="w")

        tk.Button(phase_frame, text="Set Target Phase", command=self.__on_set_target_phase).grid(row=1, column=2, padx=8, pady=8)

        tk.Label(phase_frame, text="Initial Phase", font=("Arial", 11)).grid(row=2, column=0, padx=8, pady=8, sticky="w")
        self.__initial_phase_entry = tk.Entry(phase_frame, width=16)
        self.__initial_phase_entry.insert(0, "0.0")
        self.__initial_phase_entry.grid(row=2, column=1, padx=8, pady=8, sticky="w")

        tk.Button(phase_frame, text="Set Initial Phase", command=self.__on_set_initial_phase).grid(row=2, column=2, padx=8, pady=8)

        tuning_frame = tk.LabelFrame(frame, text="Timing / Motion Config")
        tuning_frame.pack(fill=tk.X, pady=(0, 8))

        tk.Label(tuning_frame, text="Skew Rate (deg/s)", font=("Arial", 11)).grid(row=0, column=0, padx=8, pady=8, sticky="w")
        self.__skew_rate_entry = tk.Entry(tuning_frame, width=16)
        self.__skew_rate_entry.insert(0, "1.0")
        self.__skew_rate_entry.grid(row=0, column=1, padx=8, pady=8, sticky="w")
        tk.Button(tuning_frame, text="Set Skew Rate", command=self.__on_set_skew_rate).grid(row=0, column=2, padx=8, pady=8)

        tk.Label(tuning_frame, text="Laser Warmup (s)", font=("Arial", 11)).grid(row=1, column=0, padx=8, pady=8, sticky="w")
        self.__laser_warmup_entry = tk.Entry(tuning_frame, width=16)
        self.__laser_warmup_entry.insert(0, "5.0")
        self.__laser_warmup_entry.grid(row=1, column=1, padx=8, pady=8, sticky="w")
        tk.Button(tuning_frame, text="Set Laser Warmup", command=self.__on_set_laser_warmup_time).grid(row=1, column=2, padx=8, pady=8)

        tk.Label(tuning_frame, text="Chopper Startup (s)", font=("Arial", 11)).grid(row=2, column=0, padx=8, pady=8, sticky="w")
        self.__chopper_startup_entry = tk.Entry(tuning_frame, width=16)
        self.__chopper_startup_entry.insert(0, "5.0")
        self.__chopper_startup_entry.grid(row=2, column=1, padx=8, pady=8, sticky="w")
        tk.Button(tuning_frame, text="Set Chopper Startup", command=self.__on_set_chopper_startup_time).grid(row=2, column=2, padx=8, pady=8)

        tk.Label(tuning_frame, text="Chopper Target (Hz)", font=("Arial", 11)).grid(row=3, column=0, padx=8, pady=8, sticky="w")
        self.__chopper_frequency_entry = tk.Entry(tuning_frame, width=16)
        self.__chopper_frequency_entry.insert(0, "192")
        self.__chopper_frequency_entry.grid(row=3, column=1, padx=8, pady=8, sticky="w")
        tk.Button(tuning_frame, text="Set Chopper Target", command=self.__on_set_chopper_frequency).grid(row=3, column=2, padx=8, pady=8)

        control_frame = tk.LabelFrame(frame, text="Isolated Control")
        control_frame.pack(fill=tk.X, pady=(8, 8))

        tk.Button(control_frame, text="Preinit", width=14, command=self.__on_preinit).grid(row=0, column=0, padx=8, pady=8)
        tk.Button(control_frame, text="Init", width=14, command=self.__on_init).grid(row=0, column=1, padx=8, pady=8)
        tk.Button(control_frame, text="Stop", width=14, command=self.__on_stop).grid(row=0, column=2, padx=8, pady=8)

        manual_frame = tk.LabelFrame(frame, text="Manual Test Control")
        manual_frame.pack(fill=tk.X, pady=(0, 8))

        tk.Button(manual_frame, text="Laser ON", width=14, command=self.__on_laser_on).grid(row=0, column=0, padx=8, pady=8)
        tk.Button(manual_frame, text="Laser OFF", width=14, command=self.__on_laser_off).grid(row=0, column=1, padx=8, pady=8)
        tk.Button(manual_frame, text="Chopper ON", width=14, command=self.__on_chopper_on).grid(row=0, column=2, padx=8, pady=8)
        tk.Button(manual_frame, text="Chopper OFF", width=14, command=self.__on_chopper_off).grid(row=0, column=3, padx=8, pady=8)

        tk.Label(manual_frame, text="Manual Target Phase", font=("Arial", 11)).grid(row=1, column=0, padx=8, pady=8, sticky="w")
        self.__manual_phase_entry = tk.Entry(manual_frame, width=16)
        self.__manual_phase_entry.insert(0, "0.0")
        self.__manual_phase_entry.grid(row=1, column=1, padx=8, pady=8, sticky="w")
        tk.Button(manual_frame, text="Set Manual Target", command=self.__on_set_manual_phase).grid(row=1, column=2, padx=8, pady=8)

        exposure_frame = tk.LabelFrame(frame, text="Timed Exposure Control")
        exposure_frame.pack(fill=tk.X, pady=(8, 8))

        tk.Label(exposure_frame, text="Exposure Time (s)", font=("Arial", 11)).grid(row=0, column=0, padx=8, pady=8, sticky="w")
        self.__exposure_time_entry = tk.Entry(exposure_frame, width=16)
        self.__exposure_time_entry.insert(0, "1.0")
        self.__exposure_time_entry.grid(row=0, column=1, padx=8, pady=8, sticky="w")
        tk.Button(exposure_frame, text="Do Timed Exposure", command=self.__on_do_timed_exposure).grid(row=0, column=2, padx=8, pady=8)

        tk.Button(exposure_frame, text="Do Continuous", width=14, command=self.__on_do_continuous_exposure).grid(row=1, column=0, padx=8, pady=8)
        tk.Button(exposure_frame, text="Laser Shut", width=14, command=self.__on_laser_shut).grid(row=1, column=1, padx=8, pady=8)

        status_frame = tk.LabelFrame(frame, text="Laser Status")
        status_frame.pack(fill=tk.BOTH, expand=True, pady=(8, 0))

        self.__laser_label = tk.Label(status_frame, text="Laser: Unknown", font=("Arial", 12))
        self.__laser_label.pack(anchor="w", padx=8, pady=(8, 0))

        self.__chopper_label = tk.Label(status_frame, text="Chopper: Unknown", font=("Arial", 12))
        self.__chopper_label.pack(anchor="w", padx=8, pady=(4, 0))

        self.__phase_label = tk.Label(status_frame, text="Phase: current=N/A target=N/A", font=("Arial", 12))
        self.__phase_label.pack(anchor="w", padx=8, pady=(4, 0))

        self.__configured_label = tk.Label(status_frame, text="Configured: preinit=0.0 target=0.0 initial=0.0", font=("Arial", 12))
        self.__configured_label.pack(anchor="w", padx=8, pady=(4, 0))

        self.__timing_label = tk.Label(status_frame, text="Timing: skew=1.000 warmup=5.000 startup=5.000", font=("Arial", 12))
        self.__timing_label.pack(anchor="w", padx=8, pady=(4, 0))

        self.__result_label = tk.Label(status_frame, text="Last action: none", font=("Arial", 11))
        self.__result_label.pack(anchor="w", padx=8, pady=(12, 8))

    def __setup_client(self):
        c_uuid = uuid.uuid4()

        self.__logger_sock = tcp.TCPClientSocket()
        self.__logger_sock.connect(("127.0.0.1", 11751))
        self.__logger_sock.start()

        self.__logger = LogClient(self.__logger_sock, origin_uuid=c_uuid)

        def _on_ready():
            if self.__connected:
                return

            sh = self.__client.register_subsystem("__laser_sync_gui", uuid.uuid4(), temporary=True)
            self.__on_got_subsystem(sh)

        self.__client = client.DDSClient(c_uuid, logger=self.__logger, ip=ECS_IP)
        self.__client.when_ready().then(_on_ready)

    def __on_got_subsystem(self, handle: client._RegisteredSubsystemHandle):
        self.__subsystem = handle

        self.__preinit_phase_kv = handle.add_remote_kv(
            uuids.UUID_LASER_CONTROLLER,
            subsystem.KVDescriptor(types.FloatTypeSpecifier(), b"preinit_phase", False, True, True),
        )
        self.__target_phase_kv = handle.add_remote_kv(
            uuids.UUID_LASER_CONTROLLER,
            subsystem.KVDescriptor(types.FloatTypeSpecifier(), b"target_phase", False, True, True),
        )
        self.__initial_phase_kv = handle.add_remote_kv(
            uuids.UUID_LASER_CONTROLLER,
            subsystem.KVDescriptor(types.FloatTypeSpecifier(), b"initial_phase", False, True, True),
        )
        self.__skew_rate_kv = handle.add_remote_kv(
            uuids.UUID_LASER_CONTROLLER,
            subsystem.KVDescriptor(types.FloatTypeSpecifier(), b"skew_rate", False, True, True),
        )
        self.__laser_warmup_time_kv = handle.add_remote_kv(
            uuids.UUID_LASER_CONTROLLER,
            subsystem.KVDescriptor(types.FloatTypeSpecifier(), b"laser_warmup_time", False, True, True),
        )
        self.__chopper_startup_time_kv = handle.add_remote_kv(
            uuids.UUID_LASER_CONTROLLER,
            subsystem.KVDescriptor(types.FloatTypeSpecifier(), b"chopper_startup_time", False, True, True),
        )
        self.__chopper_frequency_kv = handle.add_remote_kv(
            uuids.UUID_LASER_CONTROLLER,
            subsystem.KVDescriptor(types.FloatTypeSpecifier(), b"chopper_frequency_hz", False, True, True),
        )
        self.__status_kv = handle.add_remote_kv(
            uuids.UUID_LASER_CONTROLLER,
            subsystem.KVDescriptor(types.ByteTypeSpecifier(), b"status", True, True, False),
        )
        self.__exp_state_kv = handle.add_remote_kv(
            uuids.UUID_LASER_CONTROLLER,
            subsystem.KVDescriptor(types.ByteTypeSpecifier(), b"exp_state", False, True, False),
        )

        self.__status_kv.on_new_data_received(self.__on_status_update)

        self.__preinit_event = handle.add_event_provider(b"laser_test_preinit")
        self.__init_event = handle.add_event_provider(b"laser_test_init")
        self.__stop_event = handle.add_event_provider(b"laser_test_stop")
        self.__laser_on_event = handle.add_event_provider(b"laser_test_laser_on")
        self.__laser_off_event = handle.add_event_provider(b"laser_test_laser_off")
        self.__chopper_on_event = handle.add_event_provider(b"laser_test_chopper_on")
        self.__chopper_off_event = handle.add_event_provider(b"laser_test_chopper_off")
        self.__set_phase_event = handle.add_event_provider(b"laser_test_set_phase")

        self.__do_timed_exposure_event = handle.add_event_provider(b"laser_do_timed_exposure")
        self.__do_continuous_exposure_event = handle.add_event_provider(b"laser_do_continuous_exposure")
        self.__laser_shut_event = handle.add_event_provider(b"laser_shut")

        self.__connected = True
        self.__ui_queue.put(("connected", None))
        self.__cmd_queue.put(("refresh_phases", None))
        self.__cmd_queue.put(("read_exp_state", None))

    def __on_status_update(self, value: bytes):
        try:
            status = LaserSyncStatus.decode(value)
        except (ValueError, TypeError, pickle.PickleError):
            return

        self.__ui_queue.put(("status", status))

    def __worker_thread(self, stop_flag: daemon.StopFlag):
        while stop_flag.run() and self.__run:
            try:
                cmd, payload = self.__cmd_queue.get(timeout=0.1)
            except queue.Empty:
                continue

            try:
                if cmd == "set_preinit_phase":
                    self.__set_phase_kv(self.__preinit_phase_kv, payload, "preinit")
                elif cmd == "set_target_phase":
                    self.__set_phase_kv(self.__target_phase_kv, payload, "target")
                elif cmd == "set_initial_phase":
                    self.__set_phase_kv(self.__initial_phase_kv, payload, "initial")
                elif cmd == "set_skew_rate":
                    self.__set_tuning_kv(self.__skew_rate_kv, payload, "skew_rate")
                elif cmd == "set_laser_warmup_time":
                    self.__set_tuning_kv(self.__laser_warmup_time_kv, payload, "laser_warmup_time")
                elif cmd == "set_chopper_startup_time":
                    self.__set_tuning_kv(self.__chopper_startup_time_kv, payload, "chopper_startup_time")
                elif cmd == "set_chopper_frequency":
                    self.__set_chopper_frequency(payload)
                elif cmd == "preinit":
                    self.__call_preinit()
                elif cmd == "init":
                    self.__call_init()
                elif cmd == "stop":
                    self.__call_stop()
                elif cmd == "refresh_phases":
                    self.__refresh_phase_values()
                elif cmd == "read_exp_state":
                    self.__read_exp_state()
                elif cmd == "laser_on":
                    self.__call_simple_test_event(self.__laser_on_event, "Laser ON")
                elif cmd == "laser_off":
                    self.__call_simple_test_event(self.__laser_off_event, "Laser OFF")
                elif cmd == "chopper_on":
                    self.__call_simple_test_event(self.__chopper_on_event, "Chopper ON")
                elif cmd == "chopper_off":
                    self.__call_simple_test_event(self.__chopper_off_event, "Chopper OFF")
                elif cmd == "set_manual_phase":
                    self.__call_set_manual_target(payload)
                elif cmd == "do_timed_exposure":
                    self.__call_do_timed_exposure(payload)
                elif cmd == "do_continuous_exposure":
                    self.__call_do_continuous_exposure()
                elif cmd == "laser_shut":
                    self.__call_laser_shut()
            except Exception as exc:
                self.__ui_queue.put(("result", f"Action failed: {exc}"))

    def __set_phase_kv(self, kv, value: float, label: str):
        if kv is None:
            self.__ui_queue.put(("result", "Not connected to laser subsystem."))
            return

        awaiter = kv.try_set(float(value))
        wait_for(awaiter, timeout=5.0)

        if label == "preinit":
            self.__preinit_phase_value = float(value)
        elif label == "target":
            self.__target_phase_value = float(value)
        else:
            self.__initial_phase_value = float(value)

        self.__ui_queue.put(("configured", (self.__preinit_phase_value, self.__target_phase_value, self.__initial_phase_value)))
        self.__ui_queue.put(("result", f"Set {label} phase to {value:.3f}"))

    def __set_tuning_kv(self, kv, value: float, label: str):
        if kv is None:
            self.__ui_queue.put(("result", "Not connected to laser subsystem."))
            return

        awaiter = kv.try_set(float(value))
        wait_for(awaiter, timeout=5.0)

        if label == "skew_rate":
            self.__skew_rate_value = float(value)
            msg = f"Set skew rate to {value:.3f}"
        elif label == "laser_warmup_time":
            self.__laser_warmup_time_value = float(value)
            msg = f"Set laser warmup time to {value:.3f}"
        else:
            self.__chopper_startup_time_value = float(value)
            msg = f"Set chopper startup time to {value:.3f}"

        self.__ui_queue.put(("timing", (self.__skew_rate_value, self.__laser_warmup_time_value, self.__chopper_startup_time_value)))
        self.__ui_queue.put(("result", msg))

    def __set_chopper_frequency(self, value: float):
        if self.__chopper_frequency_kv is None:
            self.__ui_queue.put(("result", "Not connected to laser subsystem."))
            return

        awaiter = self.__chopper_frequency_kv.try_set(float(value))
        wait_for(awaiter, timeout=5.0)
        self.__chopper_frequency_value = float(value)
        self.__ui_queue.put(("result", f"Set chopper controller target to {value:.0f} Hz"))

    def __call_preinit(self):
        if self.__preinit_event is None:
            self.__ui_queue.put(("result", "Preinit event provider not available."))
            return

        self.__run_event_with_feedback(self.__preinit_event, "Preinit", payload=bytes(), timeout=45.0)

    def __call_init(self):
        if self.__init_event is None:
            self.__ui_queue.put(("result", "Init event provider not available."))
            return

        self.__run_event_with_feedback(self.__init_event, "Init", payload=bytes(), timeout=45.0)

    def __call_stop(self):
        if self.__stop_event is None:
            self.__ui_queue.put(("result", "Stop event provider not available."))
            return

        self.__run_event_with_feedback(self.__stop_event, "Stop", payload=bytes(), timeout=30.0)

    def __call_simple_test_event(self, event_provider, label: str):
        if event_provider is None:
            self.__ui_queue.put(("result", f"{label} event provider not available."))
            return

        self.__run_event_with_feedback(event_provider, label, payload=bytes(), timeout=30.0)

    def __call_set_manual_target(self, phase: float):
        if self.__set_phase_event is None:
            self.__ui_queue.put(("result", "Set manual target event provider not available."))
            return

        payload = pickle.dumps(float(phase))
        self.__run_event_with_feedback(self.__set_phase_event, f"Set manual target {phase:.3f}", payload=payload, timeout=30.0)

    def __call_do_timed_exposure(self, expose_time: float):
        if self.__do_timed_exposure_event is None:
            self.__ui_queue.put(("result", "Timed exposure event provider not available."))
            return

        payload = struct.pack('d', float(expose_time))
        self.__run_event_with_feedback(self.__do_timed_exposure_event, f"Timed exposure {expose_time:.3f}s", payload=payload, timeout=expose_time + 30.0)

    def __call_do_continuous_exposure(self):
        if self.__do_continuous_exposure_event is None:
            self.__ui_queue.put(("result", "Continuous exposure event provider not available."))
            return

        self.__run_event_with_feedback(self.__do_continuous_exposure_event, "Continuous exposure", payload=bytes(), timeout=30.0)

    def __call_laser_shut(self):
        if self.__laser_shut_event is None:
            self.__ui_queue.put(("result", "Laser shut event provider not available."))
            return

        self.__run_event_with_feedback(self.__laser_shut_event, "Laser shut", payload=bytes(), timeout=30.0)

    @staticmethod
    def __decode_result_text(value) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            if value.startswith(magics.OP_OK):
                value = value[len(magics.OP_OK):]
            elif value.startswith(magics.OP_IN_PROGRESS):
                value = value[len(magics.OP_IN_PROGRESS):]
            return value.decode("utf-8", errors="replace").strip()
        return str(value).strip()

    def __run_event_with_feedback(self, event_provider, label: str, payload: bytes, timeout: float):
        handle = event_provider.call(payload, [uuids.UUID_LASER_CONTROLLER])
        if handle is None:
            self.__ui_queue.put(("result", f"{label} failed: request could not be sent."))
            return

        self.__ui_queue.put(("progress_open", f"{label} in progress..."))

        try:
            begin = time.monotonic()
            last_feedback = None

            while handle.is_in_progress() and (time.monotonic() - begin) < timeout:
                state = handle.get_state(uuids.UUID_LASER_CONTROLLER)
                result = handle.get_result(uuids.UUID_LASER_CONTROLLER)
                if state == client.EVENT_IN_PROGRESS:
                    feedback = self.__decode_result_text(result)
                    if feedback and feedback != last_feedback:
                        last_feedback = feedback
                        self.__ui_queue.put(("progress_update", feedback))
                time.sleep(0.1)
        finally:
            self.__ui_queue.put(("progress_close", None))

        if handle.is_in_progress():
            self.__ui_queue.put(("result", f"{label} failed: timed out after {timeout:.0f}s."))
            return

        state = handle.get_state(uuids.UUID_LASER_CONTROLLER)
        result = handle.get_result(uuids.UUID_LASER_CONTROLLER)
        msg = self.__decode_result_text(result)

        if state == client.EVENT_OK:
            self.__ui_queue.put(("result", msg if msg else f"{label} complete."))
            return

        self.__ui_queue.put(("result", f"{label} failed: {msg if msg else 'unknown error.'}"))

    def __refresh_phase_values(self):
        if self.__preinit_phase_kv is not None:
            p_val, _, _ = wait_for(self.__preinit_phase_kv.try_get(), timeout=5.0)
            if p_val is not None:
                self.__preinit_phase_value = p_val

        if self.__target_phase_kv is not None:
            t_val, _, _ = wait_for(self.__target_phase_kv.try_get(), timeout=5.0)
            if t_val is not None:
                self.__target_phase_value = t_val

        if self.__initial_phase_kv is not None:
            i_val, _, _ = wait_for(self.__initial_phase_kv.try_get(), timeout=5.0)
            if i_val is not None:
                self.__initial_phase_value = i_val

        if self.__skew_rate_kv is not None:
            skew, _, _ = wait_for(self.__skew_rate_kv.try_get(), timeout=5.0)
            if skew is not None:
                self.__skew_rate_value = skew

        if self.__laser_warmup_time_kv is not None:
            warmup, _, _ = wait_for(self.__laser_warmup_time_kv.try_get(), timeout=5.0)
            if warmup is not None:
                self.__laser_warmup_time_value = warmup

        if self.__chopper_startup_time_kv is not None:
            startup, _, _ = wait_for(self.__chopper_startup_time_kv.try_get(), timeout=5.0)
            if startup is not None:
                self.__chopper_startup_time_value = startup

        if self.__chopper_frequency_kv is not None:
            frequency, _, _ = wait_for(self.__chopper_frequency_kv.try_get(), timeout=5.0)
            if frequency is not None:
                self.__chopper_frequency_value = frequency

        self.__ui_queue.put(("configured", (self.__preinit_phase_value, self.__target_phase_value, self.__initial_phase_value)))
        self.__ui_queue.put(("timing", (self.__skew_rate_value, self.__laser_warmup_time_value, self.__chopper_startup_time_value)))

    def __read_exp_state(self):
        if self.__exp_state_kv is None:
            return

        val, _, _ = wait_for(self.__exp_state_kv.try_get(), timeout=5.0)
        if val is None:
            return

        ok_b, reason_b = segment_bytes.decode(val)
        ok = bool.from_bytes(ok_b, byteorder="big")
        reason = reason_b.decode("utf-8", errors="replace")
        self.__ui_queue.put(("exp_state", (ok, reason)))

    def __ui_tick(self):
        while not self.__ui_queue.empty():
            msg, payload = self.__ui_queue.get()

            if msg == "connected":
                self.__connection_label.config(text="Connection: CONNECTED")
            elif msg == "status":
                self.__current_status = payload
                self.__render_status(payload)
            elif msg == "configured":
                p_val, t_val, i_val = payload
                self.__configured_label.config(text=f"Configured: preinit={p_val:.3f} target={t_val:.3f} initial={i_val:.3f}")
            elif msg == "timing":
                skew, warmup, startup = payload
                self.__timing_label.config(text=f"Timing: skew={skew:.3f} warmup={warmup:.3f} startup={startup:.3f}")
            elif msg == "result":
                self.__result_label.config(text=f"Last action: {payload}")
            elif msg == "exp_state":
                ok, reason = payload
                status = "IN-PROGRESS" if ok else "IDLE"
                self.__exp_state_label.config(text=f"Experiment State: {status} ({reason})")
            elif msg == "progress_open":
                self.__show_progress_dialog(payload)
            elif msg == "progress_update":
                self.__update_progress_dialog(payload)
            elif msg == "progress_close":
                self.__close_progress_dialog()

        if self.__run:
            self.__root.after(100, self.__ui_tick)

    def __render_status(self, status: LaserSyncStatus):
        laser_text = "ON" if status.laser_on else "OFF"
        if status.laser_warming_up:
            laser_text += " (warming up)"
        if status.desired_laser_on is not None and status.desired_laser_on != status.laser_on:
            laser_text += " (requested ON)" if status.desired_laser_on else " (shutdown requested)"
        if status.waveform_connected is False:
            laser_text += " (generator disconnected)"

        chopper_text = "ON" if status.chopper_on else "OFF"
        if status.chopper_starting_up:
            chopper_text += " (starting up)"
        if status.chopper_spinning is True and not status.chopper_on:
            chopper_text += " (outside target tolerance)"
        if status.desired_chopper_on is not None and status.desired_chopper_on != status.chopper_on:
            chopper_text += " (requested ON)" if status.desired_chopper_on else " (shutdown requested)"
        if status.target_chopper_frequency_hz is not None:
            measured = "N/A" if status.chopper_frequency_hz is None else f"{float(status.chopper_frequency_hz):.2f} Hz"
            chopper_text += f" [measured {measured}, target {status.target_chopper_frequency_hz} Hz]"
        if status.chopper_connected is False:
            chopper_text += " (controller disconnected)"

        self.__laser_label.config(text=f"Laser: {laser_text}")
        self.__chopper_label.config(text=f"Chopper: {chopper_text}")
        self.__phase_label.config(text=f"Phase: current={status.current_phase:.3f} target={status.target_phase:.3f}")
        if status.chopper_error or status.waveform_error:
            self.__result_label.config(text=f"Hardware fault: {status.chopper_error or status.waveform_error}")

    def __periodic_refresh(self):
        if self.__run and self.__connected:
            self.__cmd_queue.put(("read_exp_state", None))
            self.__cmd_queue.put(("refresh_phases", None))

        if self.__run:
            self.__root.after(1200, self.__periodic_refresh)

    def __on_set_preinit_phase(self):
        try:
            value = float(self.__preinit_phase_entry.get().strip())
        except ValueError:
            self.__result_label.config(text="Last action: invalid preinit phase value")
            return

        self.__cmd_queue.put(("set_preinit_phase", value))

    def __on_set_target_phase(self):
        try:
            value = float(self.__target_phase_entry.get().strip())
        except ValueError:
            self.__result_label.config(text="Last action: invalid target phase value")
            return

        self.__cmd_queue.put(("set_target_phase", value))

    def __on_set_initial_phase(self):
        try:
            value = float(self.__initial_phase_entry.get().strip())
        except ValueError:
            self.__result_label.config(text="Last action: invalid initial phase value")
            return

        self.__cmd_queue.put(("set_initial_phase", value))

    def __on_set_skew_rate(self):
        try:
            value = float(self.__skew_rate_entry.get().strip())
        except ValueError:
            self.__result_label.config(text="Last action: invalid skew rate value")
            return

        self.__cmd_queue.put(("set_skew_rate", value))

    def __on_set_laser_warmup_time(self):
        try:
            value = float(self.__laser_warmup_entry.get().strip())
        except ValueError:
            self.__result_label.config(text="Last action: invalid laser warmup value")
            return

        self.__cmd_queue.put(("set_laser_warmup_time", value))

    def __on_set_chopper_startup_time(self):
        try:
            value = float(self.__chopper_startup_entry.get().strip())
        except ValueError:
            self.__result_label.config(text="Last action: invalid chopper startup value")
            return

        self.__cmd_queue.put(("set_chopper_startup_time", value))

    def __on_set_chopper_frequency(self):
        try:
            value = float(self.__chopper_frequency_entry.get().strip())
        except ValueError:
            self.__result_label.config(text="Last action: invalid chopper target value")
            return

        self.__cmd_queue.put(("set_chopper_frequency", value))

    def __on_preinit(self):
        self.__cmd_queue.put(("preinit", None))

    def __on_init(self):
        self.__cmd_queue.put(("init", None))

    def __on_stop(self):
        self.__cmd_queue.put(("stop", None))

    def __on_laser_on(self):
        self.__cmd_queue.put(("laser_on", None))

    def __on_laser_off(self):
        self.__cmd_queue.put(("laser_off", None))

    def __on_chopper_on(self):
        self.__cmd_queue.put(("chopper_on", None))

    def __on_chopper_off(self):
        self.__cmd_queue.put(("chopper_off", None))

    def __on_set_manual_phase(self):
        try:
            value = float(self.__manual_phase_entry.get().strip())
        except ValueError:
            self.__result_label.config(text="Last action: invalid manual target phase value")
            return

        self.__cmd_queue.put(("set_manual_phase", value))

    def __on_do_timed_exposure(self):
        try:
            value = float(self.__exposure_time_entry.get().strip())
        except ValueError:
            self.__result_label.config(text="Last action: invalid exposure time value")
            return

        self.__cmd_queue.put(("do_timed_exposure", value))

    def __on_do_continuous_exposure(self):
        self.__cmd_queue.put(("do_continuous_exposure", None))

    def __on_laser_shut(self):
        self.__cmd_queue.put(("laser_shut", None))

    def ok(self):
        return self.__run and self.__client.ok() and self.__daemon.is_ok()

    def close(self):
        self.__run = False

        self.__close_progress_dialog()

        if self.__daemon is not None:
            self.__daemon.stop()

        if self.__client is not None:
            self.__client.close()

        if self.__logger_sock is not None:
            self.__logger_sock.close()

    def __show_progress_dialog(self, text: str):
        if self.__progress_dialog is None:
            dialog = tk.Toplevel(self.__dialog_parent)
            dialog.title("Laser Sync Progress")
            dialog.geometry("460x120")
            dialog.transient(self.__dialog_parent)
            dialog.grab_set()
            dialog.protocol("WM_DELETE_WINDOW", lambda: None)

            self.__progress_text = tk.StringVar(value=text)
            label = tk.Label(dialog, textvariable=self.__progress_text, font=("Arial", 11), wraplength=430, justify="left")
            label.pack(fill=tk.BOTH, expand=True, padx=14, pady=14)
            self.__progress_dialog = dialog
            return

        self.__update_progress_dialog(text)

    def __update_progress_dialog(self, text: str):
        if self.__progress_dialog is None or self.__progress_text is None:
            return
        self.__progress_text.set(text)

    def __close_progress_dialog(self):
        if self.__progress_dialog is None:
            return
        try:
            self.__progress_dialog.grab_release()
        except tk.TclError:
            pass
        self.__progress_dialog.destroy()
        self.__progress_dialog = None
        self.__progress_text = None


def main(_args: argparse.Namespace):
    root = tk.Tk()
    app = LaserSyncTestGUI(root)

    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass
    finally:
        app.close()

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Laser sync isolated test GUI")
    args = parser.parse_args()
    sys.exit(main(args))
