import argparse
import pickle
import queue
import sys
import time
import tkinter as tk
import uuid

import segment_bytes

from ipi_ecs.core import daemon
import ipi_ecs.core.tcp as tcp
import ipi_ecs.dds.client as client
import ipi_ecs.dds.magics as magics
import ipi_ecs.dds.subsystem as subsystem
import ipi_ecs.dds.types as types
from ipi_ecs.logging.client import LogClient
from ipi_ecs.cli.captive_cli import wait_for, wait_for_event

from chamber_ctl.subsystems import uuids
from chamber_ctl.subsystems.laser import LaserSyncStatus


class LaserSyncTestGUI:
    def __init__(self, root: tk.Tk):
        self.__root = root
        self.__run = True
        self.__connected = False

        self.__client = None
        self.__subsystem = None

        self.__logger_sock = None
        self.__logger = None

        self.__preinit_phase_kv = None
        self.__target_phase_kv = None
        self.__status_kv = None
        self.__exp_state_kv = None

        self.__preinit_event = None
        self.__init_event = None
        self.__stop_event = None

        self.__current_status = None
        self.__ui_queue = queue.Queue()
        self.__cmd_queue = queue.Queue()

        self.__preinit_phase_value = 0.0
        self.__target_phase_value = 0.0

        self.__build_ui(root)
        self.__setup_client()

        self.__daemon = daemon.Daemon()
        self.__daemon.add(self.__worker_thread)
        self.__daemon.start()

        self.__root.after(100, self.__ui_tick)
        self.__root.after(800, self.__periodic_refresh)

    def __build_ui(self, root: tk.Tk):
        root.title("Laser Sync Test")
        root.geometry("900x600")

        frame = tk.Frame(root)
        frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)

        title = tk.Label(frame, text="Laser Sync Isolated Test", font=("Arial", 24, "bold"))
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

        control_frame = tk.LabelFrame(frame, text="Isolated Control")
        control_frame.pack(fill=tk.X, pady=(8, 8))

        tk.Button(control_frame, text="Preinit", width=14, command=self.__on_preinit).grid(row=0, column=0, padx=8, pady=8)
        tk.Button(control_frame, text="Init", width=14, command=self.__on_init).grid(row=0, column=1, padx=8, pady=8)
        tk.Button(control_frame, text="Stop", width=14, command=self.__on_stop).grid(row=0, column=2, padx=8, pady=8)

        status_frame = tk.LabelFrame(frame, text="Laser Status")
        status_frame.pack(fill=tk.BOTH, expand=True, pady=(8, 0))

        self.__laser_label = tk.Label(status_frame, text="Laser: Unknown", font=("Arial", 12))
        self.__laser_label.pack(anchor="w", padx=8, pady=(8, 0))

        self.__chopper_label = tk.Label(status_frame, text="Chopper: Unknown", font=("Arial", 12))
        self.__chopper_label.pack(anchor="w", padx=8, pady=(4, 0))

        self.__phase_label = tk.Label(status_frame, text="Phase: current=N/A target=N/A", font=("Arial", 12))
        self.__phase_label.pack(anchor="w", padx=8, pady=(4, 0))

        self.__configured_label = tk.Label(status_frame, text="Configured: preinit=0.0 target=0.0", font=("Arial", 12))
        self.__configured_label.pack(anchor="w", padx=8, pady=(4, 0))

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

        self.__client = client.DDSClient(c_uuid, logger=self.__logger)
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
        else:
            self.__target_phase_value = float(value)

        self.__ui_queue.put(("configured", (self.__preinit_phase_value, self.__target_phase_value)))
        self.__ui_queue.put(("result", f"Set {label} phase to {value:.3f}"))

    def __call_preinit(self):
        if self.__preinit_event is None:
            self.__ui_queue.put(("result", "Preinit event provider not available."))
            return

        awaiter = self.__preinit_event.call(bytes(), [uuids.UUID_LASER_CONTROLLER]).after()
        r_value, _r_state, r_reason = wait_for_event(awaiter, uuids.UUID_LASER_CONTROLLER, timeout=30.0)

        if r_value is None or not r_value.startswith(magics.OP_OK):
            self.__ui_queue.put(("result", f"Preinit failed: {r_reason}"))
            return

        self.__ui_queue.put(("result", "Preinit complete."))

    def __call_init(self):
        if self.__init_event is None:
            self.__ui_queue.put(("result", "Init event provider not available."))
            return

        awaiter = self.__init_event.call(bytes(), [uuids.UUID_LASER_CONTROLLER]).after()
        r_value, _r_state, r_reason = wait_for_event(awaiter, uuids.UUID_LASER_CONTROLLER, timeout=30.0)

        if r_value is None or not r_value.startswith(magics.OP_OK):
            self.__ui_queue.put(("result", f"Init failed: {r_reason}"))
            return

        self.__ui_queue.put(("result", "Init complete."))

    def __call_stop(self):
        if self.__stop_event is None:
            self.__ui_queue.put(("result", "Stop event provider not available."))
            return

        awaiter = self.__stop_event.call(bytes(), [uuids.UUID_LASER_CONTROLLER]).after()
        r_value, _r_state, r_reason = wait_for_event(awaiter, uuids.UUID_LASER_CONTROLLER, timeout=15.0)

        if r_value is None or not r_value.startswith(magics.OP_OK):
            self.__ui_queue.put(("result", f"Stop failed: {r_reason}"))
            return

        self.__ui_queue.put(("result", "Stop complete."))

    def __refresh_phase_values(self):
        if self.__preinit_phase_kv is not None:
            p_val, _, _ = wait_for(self.__preinit_phase_kv.try_get(), timeout=5.0)
            if p_val is not None:
                self.__preinit_phase_value = p_val

        if self.__target_phase_kv is not None:
            t_val, _, _ = wait_for(self.__target_phase_kv.try_get(), timeout=5.0)
            if t_val is not None:
                self.__target_phase_value = t_val

        self.__ui_queue.put(("configured", (self.__preinit_phase_value, self.__target_phase_value)))

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
                p_val, t_val = payload
                self.__configured_label.config(text=f"Configured: preinit={p_val:.3f} target={t_val:.3f}")
            elif msg == "result":
                self.__result_label.config(text=f"Last action: {payload}")
            elif msg == "exp_state":
                ok, reason = payload
                status = "IN-PROGRESS" if ok else "IDLE"
                self.__exp_state_label.config(text=f"Experiment State: {status} ({reason})")

        if self.__run:
            self.__root.after(100, self.__ui_tick)

    def __render_status(self, status: LaserSyncStatus):
        laser_text = "ON" if status.laser_on else "OFF"
        if status.laser_warming_up:
            laser_text += " (warming up)"

        chopper_text = "ON" if status.chopper_on else "OFF"
        if status.chopper_starting_up:
            chopper_text += " (starting up)"

        self.__laser_label.config(text=f"Laser: {laser_text}")
        self.__chopper_label.config(text=f"Chopper: {chopper_text}")
        self.__phase_label.config(text=f"Phase: current={status.current_phase:.3f} target={status.target_phase:.3f}")

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

    def __on_preinit(self):
        self.__cmd_queue.put(("preinit", None))

    def __on_init(self):
        self.__cmd_queue.put(("init", None))

    def __on_stop(self):
        self.__cmd_queue.put(("stop", None))

    def ok(self):
        return self.__run and self.__client.ok() and self.__daemon.is_ok()

    def close(self):
        self.__run = False

        if self.__daemon is not None:
            self.__daemon.stop()

        if self.__client is not None:
            self.__client.close()

        if self.__logger_sock is not None:
            self.__logger_sock.close()


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
