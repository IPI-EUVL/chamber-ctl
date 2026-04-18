import argparse
import math
import queue
import time
import tkinter as tk
import uuid
from math import cos, sin, radians
from tkinter import ttk

import ipi_ecs.core.tcp as tcp
import ipi_ecs.dds.client as client
import ipi_ecs.dds.subsystem as subsystem
import ipi_ecs.dds.types as types
from ipi_ecs.cli.captive_cli import wait_for
from ipi_ecs.core import daemon
from ipi_ecs.logging.client import LogClient

import chamber_ctl.subsystems.sample_motion as stage_client
from chamber_ctl.subsystems import uuids

LIN_LENGTH = 90


def rotated_rectangle_coords(cx, cy, width, height, angle_deg):
    angle_rad = math.radians(angle_deg)
    cos_a = math.cos(angle_rad)
    sin_a = math.sin(angle_rad)

    hw, hh = width / 2, height / 2
    corners = [(-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)]

    rotated = []
    for x, y in corners:
        xr = cx + x * cos_a - y * sin_a
        yr = cy + x * sin_a + y * cos_a
        rotated.extend((xr, yr))

    return rotated


class SampleMotionDDSClient:
    def __init__(self):
        self.__run = True
        self.__connected = False
        self.__did_config = False

        self.__subsystem = None

        self.__position_kv = None
        self.__offset_kv = None
        self.__sample_kv = None
        self.__enabled_kv = None
        self.__status_kv = None

        self.__goto_sample_event = None
        self.__goto_event = None
        self.__home_event = None
        self.__home_rot_event = None

        self.__position = (0.0, 0.0)
        self.__offset = (0.0, 0.0)
        self.__sample = -1
        self.__enabled = 0
        self.__status = stage_client.STATE_OFFLINE

        self.__result = "Disconnected"
        self.__event_active = False
        self.__event_progress_text = ""
        self.__event_result_text = ""
        self.__event_result_success = False

        self.__cmd_queue = queue.Queue()

        c_uuid = uuid.uuid4()
        self.__logger_sock = tcp.TCPClientSocket()
        self.__logger_sock.connect(("127.0.0.1", 11751))
        self.__logger_sock.start()

        self.__logger = LogClient(self.__logger_sock, origin_uuid=c_uuid)

        self.__client = client.DDSClient(c_uuid, logger=self.__logger)
        self.__client.when_ready().then(self.__on_ready)

        self.__daemon = daemon.Daemon()
        self.__daemon.add(self.__worker)
        self.__daemon.start()

    def __on_ready(self, _=None):
        if self.__did_config:
            return

        self.__did_config = True
        sh = self.__client.register_subsystem("__sample_motion_gui", uuid.uuid4(), temporary=True)
        self.__on_got_subsystem(sh)

    def __on_got_subsystem(self, handle: client._RegisteredSubsystemHandle):
        self.__subsystem = handle

        self.__sample_kv = handle.add_remote_kv(
            uuids.UUID_SAMPLE_MOTION_CONTROLLER,
            subsystem.KVDescriptor(types.IntegerTypeSpecifier(), b"sample", False, True, False),
        )
        self.__position_kv = handle.add_remote_kv(
            uuids.UUID_SAMPLE_MOTION_CONTROLLER,
            subsystem.KVDescriptor(types.VectorTypeSpecifier(types.FloatTypeSpecifier(), 2), b"position", True, True, False),
        )
        self.__position_kv.on_new_data_received(self.__on_position)
        self.__offset_kv = handle.add_remote_kv(
            uuids.UUID_SAMPLE_MOTION_CONTROLLER,
            subsystem.KVDescriptor(types.VectorTypeSpecifier(types.FloatTypeSpecifier(), 2), b"offset", False, True, True),
        )
        self.__enabled_kv = handle.add_remote_kv(
            uuids.UUID_SAMPLE_MOTION_CONTROLLER,
            subsystem.KVDescriptor(types.IntegerTypeSpecifier(), b"enabled", True, True, False),
        )
        self.__enabled_kv.on_new_data_received(self.__on_enabled)

        self.__status_kv = handle.add_remote_kv(
            uuids.UUID_SAMPLE_MOTION_CONTROLLER,
            subsystem.KVDescriptor(types.IntegerTypeSpecifier(), b"status", True, True, False),
        )
        self.__status_kv.on_new_data_received(self.__on_status)

        self.__goto_sample_event = handle.add_event_provider(b"goto_sample")
        self.__goto_sample_event.set_types(types.IntegerTypeSpecifier(), types.ByteTypeSpecifier())

        self.__goto_event = handle.add_event_provider(b"goto")
        self.__goto_event.set_types(types.VectorTypeSpecifier(types.FloatTypeSpecifier(), 2), types.ByteTypeSpecifier())

        self.__home_event = handle.add_event_provider(b"home_sample")
        self.__home_rot_event = handle.add_event_provider(b"home_rot_sample")

        self.__connected = True
        self.__result = "Connected"

    def __on_enabled(self, value: int):
        self.__enabled = int(value)

    def __on_position(self, value: list[float]):
        if value is None or len(value) != 2:
            return
        
        self.__position = (float(value[0]), float(value[1]))

    def __on_status(self, value: int):
        self.__status = int(value)

    def __poll_kv(self, kv):
        if kv is None:
            return None

        awaiter = kv.try_get()
        if awaiter is None:
            return None

        value, _, _ = wait_for(awaiter, timeout=1.0)
        return value

    @staticmethod
    def __decode_event_result(handle):
        if handle is None:
            return "request failed"

        state = handle.get_state(uuids.UUID_SAMPLE_MOTION_CONTROLLER)
        result = handle.get_result(uuids.UUID_SAMPLE_MOTION_CONTROLLER)

        if result is None:
            return f"event state={state}"

        if isinstance(result, bytes):
            return result.decode("utf-8", errors="replace")

        return str(result)

    @staticmethod
    def __decode_event_feedback(handle):
        if handle is None:
            return ""

        result = handle.get_result(uuids.UUID_SAMPLE_MOTION_CONTROLLER)
        if result is None:
            return ""

        if isinstance(result, bytes):
            return result.decode("utf-8", errors="replace")

        return str(result)

    def __set_event_state(self, active: bool, progress_text: str = "", result_text: str = "", success: bool = False):
        self.__event_active = active
        self.__event_progress_text = progress_text
        if result_text:
            self.__event_result_text = result_text
        if not active:
            self.__event_result_success = success

    def __worker(self, stop_flag: daemon.StopFlag):
        last_poll = 0.0

        while stop_flag.run() and self.__run:
            now = time.monotonic()

            if self.__connected and (now - last_poll) >= 0.2:
                try:
                    o = self.__poll_kv(self.__offset_kv)
                    if o is not None:
                        self.__offset = (float(o[0]), float(o[1]))

                    s = self.__poll_kv(self.__sample_kv)
                    if s is not None:
                        self.__sample = int(s)
                except Exception as exc:
                    self.__result = f"poll failed: {exc}"

                last_poll = now

            try:
                cmd, payload = self.__cmd_queue.get(timeout=0.05)
            except queue.Empty:
                continue

            try:
                if cmd == "goto_position" and self.__goto_event is not None:
                    self.__set_event_state(True, "Goto in progress...")
                    evt_handle = self.__goto_event.call([float(payload[0]), float(payload[1])], [uuids.UUID_SAMPLE_MOTION_CONTROLLER])
                    if evt_handle is None:
                        self.__result = "goto send failed"
                        self.__set_event_state(False, result_text=self.__result, success=False)
                        continue

                    start = time.monotonic()
                    while evt_handle.is_in_progress() and (time.monotonic() - start) < 240.0:
                        feedback = self.__decode_event_feedback(evt_handle)
                        if feedback:
                            self.__set_event_state(True, feedback)
                        time.sleep(0.1)

                    self.__result = self.__decode_event_result(evt_handle)
                    ok = evt_handle.get_state(uuids.UUID_SAMPLE_MOTION_CONTROLLER) == client.EVENT_OK
                    self.__set_event_state(False, result_text=self.__result, success=ok)
                elif cmd == "offset" and self.__offset_kv is not None:
                    awaiter = self.__offset_kv.try_set([float(payload[0]), float(payload[1])])
                    wait_for(awaiter, timeout=5.0)
                    self.__result = "offset updated"
                elif cmd == "goto_sample" and self.__goto_sample_event is not None:
                    self.__set_event_state(True, "Goto sample in progress...")
                    evt_handle = self.__goto_sample_event.call(int(payload), [uuids.UUID_SAMPLE_MOTION_CONTROLLER])
                    if evt_handle is None:
                        self.__result = "goto_sample send failed"
                        self.__set_event_state(False, result_text=self.__result, success=False)
                        continue

                    start = time.monotonic()
                    while evt_handle.is_in_progress() and (time.monotonic() - start) < 240.0:
                        feedback = self.__decode_event_feedback(evt_handle)
                        if feedback:
                            self.__set_event_state(True, feedback)
                        time.sleep(0.1)

                    self.__result = self.__decode_event_result(evt_handle)
                    ok = evt_handle.get_state(uuids.UUID_SAMPLE_MOTION_CONTROLLER) == client.EVENT_OK
                    self.__set_event_state(False, result_text=self.__result, success=ok)
                elif cmd == "home" and self.__home_event is not None:
                    evt_handle = self.__home_event.call(bytes(), [uuids.UUID_SAMPLE_MOTION_CONTROLLER])
                    if evt_handle is None:
                        self.__result = "home send failed"
                        continue

                    start = time.monotonic()
                    while evt_handle.is_in_progress() and (time.monotonic() - start) < 240.0:
                        time.sleep(0.1)

                    self.__result = self.__decode_event_result(evt_handle)
                elif cmd == "home_rot" and self.__home_rot_event is not None:
                    evt_handle = self.__home_rot_event.call(bytes(), [uuids.UUID_SAMPLE_MOTION_CONTROLLER])
                    if evt_handle is None:
                        self.__result = "home rot send failed"
                        continue

                    start = time.monotonic()
                    while evt_handle.is_in_progress() and (time.monotonic() - start) < 240.0:
                        time.sleep(0.1)

                    self.__result = self.__decode_event_result(evt_handle)
            except Exception as exc:
                self.__result = f"command failed: {exc}"
                self.__set_event_state(False, result_text=self.__result, success=False)

    def move_to(self, th, z):
        self.__cmd_queue.put(("goto_position", (float(th), float(z))))

    def set_offset(self, x, z):
        self.__cmd_queue.put(("offset", (float(x), float(z))))

    def goto_sample(self, slot):
        self.__cmd_queue.put(("goto_sample", int(slot)))

    def home(self):
        self.__cmd_queue.put(("home", None))

    def home_rot(self):
        self.__cmd_queue.put(("home_rot", None))

    def get_position(self):
        return self.__position

    def get_offset(self):
        return self.__offset

    def get_sample(self):
        return self.__sample

    def get_state(self):
        return self.__status

    def is_enabled(self):
        return self.__enabled == 1

    def is_at_limit(self):
        return self.__position[1] >= 89.45

    def get_result_text(self):
        return self.__result

    def get_event_progress(self):
        return (self.__event_active, self.__event_progress_text, self.__event_result_text, self.__event_result_success)

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

def draw_sample_platform(canvas, stage_x, center_y, vis_scale, platform_color, samples, ring_radii, current_angle, highlight_sample=None):
    platform_radius = 50 * vis_scale
    canvas.create_oval(
        stage_x - platform_radius,
        center_y - platform_radius,
        stage_x + platform_radius,
        center_y + platform_radius,
        fill=platform_color,
        outline="black",
    )

    canvas.create_oval(stage_x - 5, center_y - 5, stage_x + 5, center_y + 5, fill="blue")

    do_highlight = time.monotonic() % 1 > 0.5

    for ring, radius_mm in ring_radii.items():
        radius_px = radius_mm * vis_scale
        canvas.create_oval(
            stage_x - radius_px,
            center_y - radius_px,
            stage_x + radius_px,
            center_y + radius_px,
            outline="gray",
            width=2,
        )

    s_i = 1
    for sample in samples:
        display_angle = sample["angle"] - current_angle
        radius = sample["radius"] * vis_scale
        x = stage_x + radius * cos(math.radians(display_angle))
        y = center_y + radius * sin(math.radians(display_angle))

        side = math.sqrt(sample["size"]) * 10 * vis_scale / 1.75
        coords = rotated_rectangle_coords(x, y, side * 2, side * 2, -current_angle)
        h_fill = "yellow" if do_highlight else "blue"
        fill = h_fill if s_i == highlight_sample else "lightgreen"

        canvas.create_polygon(coords, fill=fill, outline="black")
        canvas.create_text(x, y, text=sample['label'], font=("Helvetica", 24, "bold"))

        s_i += 1

def draw_sample_stage(canvas, center_x, center_y, vis_scale, lin_pos, current_angle, samples, ring_radii, exposure_x, exposure_y, platform_color, highlight_sample=None):
    canvas.create_rectangle(
        center_x,
        center_y - 17 * vis_scale,
        center_x + (LIN_LENGTH * vis_scale),
        center_y + 17 * vis_scale,
        fill="lightgray",
        outline="black",
    )

    canvas.create_text(exposure_x, exposure_y + 30, text="EXPOSURE", fill="red", angle=0, anchor="n", width=250)

    stage_x = center_x + lin_pos * vis_scale

    draw_sample_platform(canvas, stage_x, center_y, vis_scale, platform_color, samples, ring_radii, current_angle, highlight_sample)

    canvas.create_oval(exposure_x - 15, exposure_y - 15, exposure_x + 15, exposure_y + 15, fill="red")
    canvas.create_text(center_x - 300, center_y, text="\n".join("DOOR"), fill="red", angle=0, font=("Helvetica", 56, "bold"))

def build_sample_data():
    samples = []
    inner_radius_mm = 0.835 * 25.4
    outer_radius_mm = 1.645 * 25.4
    s_i = 0
    #Outer targets (processed first)
    for quadrant in range(4):
        base_angle = 90 * quadrant
        for i, offset in enumerate([21.04, 68.96]):
            angle = base_angle + offset
            samples.append({
                'ring': 2,
                'position': quadrant * 2 + i,
                'angle': angle,
                'label': f"{s_i + 1}",
                'radius': outer_radius_mm,
                'shape': 'square',
                'size': 2,
                'exposed': False,
                'exposure_time': 5.0
            })
            s_i += 1

    #Inner targets (processed second)
    for quadrant in range(4):
        angle = 45 + 90 * quadrant
        samples.append({
            'ring': 1,
            'position': quadrant,
            'angle': angle,
            'label': f"{s_i + 1}",
            'radius': inner_radius_mm,
            'shape': 'square',
            'size': 2,
            'exposed': False,
            'exposure_time': 5.0
        })
        s_i += 1
    
    """num = 0
    for i in [11, 4, 10, 3, 0, 5, 9, 2, 1, 6, 8, 7]:
        s = samples[i]
        s["label"] += f"\nSample #{num + 1}"
        num += 1"""

    return samples

RING_RADII = {1: 21.209, 2: 41.783}
EXPOSURE_POS = 100

class SampleStageControl:
    def __init__(self, root, ctl: SampleMotionDDSClient, own_window: bool = True):
        self.root = root
        self.__own_window = own_window
        self.__dialog_parent = root if own_window else root.winfo_toplevel()
        if self.__own_window and hasattr(root, "title"):
            root.title("Sample Stage Control")

        self.__ctl = ctl

        self.set_param()

        self.current_angle = 0
        self.current_linear_pos = 50

        self.target_z = 0
        self.target_th = 0
        self.target_sample = 1

        self.__c_sample = 0
        self.__xoff = 0
        self.__zoff = 0
        self.__progress_dialog = None
        self.__progress_text = None
        self.__progress_bar = None
        self.__progress_ok_button = None
        self.__progress_showing_result = False
        self.__progress_auto_close_after_id = None

        self.samples = build_sample_data()
        

        self.__initialize_component()
        self.handle_window()
        self.__draw_stage()
        self.gui_q_processing()

    def __ui_manual_motion(self):
        z = float(self.z_target.get())
        th = float(self.th_target.get())
        self.__ctl.move_to(th, z)

    def __ui_sample_motion(self):
        slot = int(self.sample_target.get()) - 1
        slot = max(0, min(slot, len(self.samples) - 1))

        self.__xoff = float(self.off_x.get())
        self.__zoff = float(self.off_z.get())
        self.__c_sample = slot

        self.__ctl.set_offset(self.__xoff, self.__zoff)
        self.__ctl.goto_sample(slot)

    def __ui_nudge(self, off):
        self.__xoff += off[0]
        self.__zoff += off[1]

        self.off_x.delete(0, tk.END)
        self.off_x.insert(0, str(self.__xoff))
        self.off_z.delete(0, tk.END)
        self.off_z.insert(0, str(self.__zoff))

        self.__ctl.set_offset(self.__xoff, self.__zoff)
        self.__ctl.goto_sample(self.__c_sample)

    def __ui_change_sample(self, step):
        self.__c_sample = (self.__c_sample + step) % len(self.samples)
        self.sample_target.delete(0, tk.END)
        self.sample_target.insert(0, str(self.__c_sample + 1))
        self.__ctl.goto_sample(self.__c_sample)

    def gui_q_processing(self):
        self.current_angle, self.current_linear_pos = self.__ctl.get_position()
        self.current_angle *= (360 / (math.pi * 2))

        self.update_gui_position()

        state = self.__ctl.get_state()

        status_str = ""
        if state == stage_client.STATE_IDLE:
            status_str = "Idle"
        elif state == stage_client.STATE_MOVING:
            status_str = "Moving"
        elif state == stage_client.STATE_HOMING:
            status_str = "Homing"
        elif state == stage_client.STATE_OFFLINE:
            status_str = "Offline"

        if self.__ctl.is_enabled():
            status_str += ", actuators enabled."
        else:
            status_str += ", actuators DISABLED."

        status_str += f" Last action: {self.__ctl.get_result_text()}"
        self.update_status(status_str)

        active, progress_text, result_text, result_success = self.__ctl.get_event_progress()
        if active:
            self.__show_progress_dialog(progress_text if progress_text else "Operation in progress...")
        elif self.__progress_dialog is not None:
            self.__show_progress_result(result_text if result_text else self.__ctl.get_result_text(), result_success)

        self.root.after(50, self.gui_q_processing)

    def __show_progress_dialog(self, text: str):
        if self.__progress_auto_close_after_id is not None:
            self.root.after_cancel(self.__progress_auto_close_after_id)
            self.__progress_auto_close_after_id = None

        if self.__progress_dialog is None:
            dialog = tk.Toplevel(self.__dialog_parent)
            dialog.title("Sample Motion Progress")
            dialog.geometry("520x140")
            dialog.transient(self.__dialog_parent)
            dialog.protocol("WM_DELETE_WINDOW", self.__close_progress_dialog)

            frame = ttk.Frame(dialog, padding=12)
            frame.pack(fill=tk.BOTH, expand=True)

            self.__progress_text = tk.StringVar(value=text)
            label = ttk.Label(frame, textvariable=self.__progress_text, wraplength=480, justify="left")
            label.pack(fill=tk.X, pady=(0, 8))

            self.__progress_bar = ttk.Progressbar(frame, mode="indeterminate")
            self.__progress_bar.pack(fill=tk.X, pady=(0, 10))
            self.__progress_bar.start(10)

            self.__progress_ok_button = ttk.Button(frame, text="OK", command=self.__close_progress_dialog)
            self.__progress_ok_button.pack(anchor="e")
            self.__progress_ok_button.pack_forget()

            self.__progress_dialog = dialog
            self.__progress_showing_result = False
            return

        self.__progress_showing_result = False
        if self.__progress_text is not None:
            self.__progress_text.set(text)
        if self.__progress_bar is not None:
            self.__progress_bar.start(10)
        if self.__progress_ok_button is not None:
            self.__progress_ok_button.pack_forget()

    def __show_progress_result(self, text: str, success: bool):
        if self.__progress_dialog is None:
            return

        if self.__progress_showing_result:
            return

        self.__progress_showing_result = True
        if self.__progress_text is not None:
            self.__progress_text.set(f"Operation result: {text}")
        if self.__progress_bar is not None:
            self.__progress_bar.stop()
        if self.__progress_ok_button is not None:
            self.__progress_ok_button.pack(anchor="e")

        if success:
            self.__progress_auto_close_after_id = self.root.after(2000, self.__close_progress_dialog)

    def __close_progress_dialog(self):
        if self.__progress_dialog is None:
            return

        if self.__progress_bar is not None:
            self.__progress_bar.stop()

        try:
            self.__progress_dialog.destroy()
        except tk.TclError:
            pass

        self.__progress_dialog = None
        self.__progress_text = None
        self.__progress_bar = None
        self.__progress_ok_button = None
        self.__progress_showing_result = False
        if self.__progress_auto_close_after_id is not None:
            self.root.after_cancel(self.__progress_auto_close_after_id)
            self.__progress_auto_close_after_id = None

    def set_param(self):
        self.vis_scale = 3
        self.center_x, self.center_y = 150, 350

    def _styled_labeled_entry(self, parent, label_text, default_value, attr_name, row):
        label = ttk.Label(parent, text=label_text, font=("Segoe UI", 10, "bold"))
        label.grid(row=row * 2, column=0, sticky="w", pady=(0, 2))

        entry = ttk.Entry(parent, font=("Segoe UI", 10), width=20)
        entry.insert(0, str(default_value))
        entry.grid(row=row * 2 + 1, column=0, sticky="ew", pady=(0, 10))

        setattr(self, attr_name, entry)

    def __initialize_component(self):
        main_frame = ttk.Frame(self.root, padding=10)
        main_frame.pack(fill=tk.BOTH, expand=True)

        left_container = ttk.Frame(main_frame)
        left_container.grid(row=0, column=0, sticky="nsew", padx=5)

        canvas = tk.Canvas(left_container)
        scrollbar = ttk.Scrollbar(left_container, orient="vertical", command=canvas.yview)
        scrollable_frame = ttk.Frame(canvas)

        scrollable_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        control_frame = ttk.LabelFrame(scrollable_frame, text="Stage Control Panel", padding=10)
        control_frame.pack(fill=tk.BOTH, expand=True)

        manual_motion_frame = ttk.LabelFrame(control_frame, text="Manual Motion", padding=10)
        manual_motion_frame.pack(fill=tk.X, pady=(0, 10))

        self._styled_labeled_entry(manual_motion_frame, "Target Z", self.target_z, attr_name="z_target", row=0)
        self._styled_labeled_entry(manual_motion_frame, "Target Theta", self.target_th, attr_name="th_target", row=1)
        ttk.Button(manual_motion_frame, text="GOTO", command=self.__ui_manual_motion).grid(row=0, column=2, padx=5, sticky="ew")

        sample_motion_frame = ttk.LabelFrame(control_frame, text="Sample Select", padding=10)
        sample_motion_frame.pack(fill=tk.X, pady=(0, 10))

        self._styled_labeled_entry(sample_motion_frame, "Target Sample", self.target_sample, attr_name="sample_target", row=0)
        self._styled_labeled_entry(sample_motion_frame, "Offset X", 0, attr_name="off_x", row=1)
        self._styled_labeled_entry(sample_motion_frame, "Offset Z", 0, attr_name="off_z", row=2)
        ttk.Button(sample_motion_frame, text="GOTO SAMPLE", command=self.__ui_sample_motion).grid(row=0, column=2, padx=5, sticky="ew")

        nudge_frame = ttk.LabelFrame(control_frame, text="Nudge", padding=10)
        nudge_frame.pack(fill=tk.X, pady=(0, 10))
        ttk.Button(nudge_frame, text="UP", command=lambda: self.__ui_nudge([0, 1])).grid(row=0, column=1, padx=5)
        ttk.Button(nudge_frame, text="DOWN", command=lambda: self.__ui_nudge([0, -1])).grid(row=2, column=1, padx=5)
        ttk.Button(nudge_frame, text="LEFT", command=lambda: self.__ui_nudge([-1, 0])).grid(row=1, column=0, padx=5)
        ttk.Button(nudge_frame, text="RIGHT", command=lambda: self.__ui_nudge([1, 0])).grid(row=1, column=2, padx=5)
        ttk.Button(nudge_frame, text="NEXT", command=lambda: self.__ui_change_sample(1)).grid(row=2, column=2, padx=5)
        ttk.Button(nudge_frame, text="PREV", command=lambda: self.__ui_change_sample(-1)).grid(row=2, column=0, padx=5)

        canvas.config(scrollregion=canvas.bbox("all"))

        status_frame = ttk.LabelFrame(control_frame, text="Stage Status", padding=10)
        status_frame.pack(fill=tk.X, pady=(0, 10))

        self.position_label = ttk.Label(status_frame, text="ROT: 0.0 deg, LIN: 0.0 mm", font=("Segoe UI", 11, "bold"))
        self.position_label.pack(anchor="w", pady=5)

        self.status_label = ttk.Label(status_frame, text="Disconnected", foreground="red", font=("Segoe UI", 10, "italic"))
        self.status_label.pack(anchor="w", pady=(5, 0))

        button_frame = ttk.LabelFrame(main_frame, text="Controls", padding=10)
        button_frame.grid(row=1, column=0, sticky="nsew", padx=5, pady=5)
        button_frame.columnconfigure((0, 1, 2, 3), weight=1)

        ttk.Button(button_frame, text="HOME", command=self.__ctl.home).grid(row=0, column=0, padx=5, sticky="ew")
        ttk.Button(button_frame, text="HOME ROT", command=self.__ctl.home_rot).grid(row=0, column=1, padx=5, sticky="ew")

        vis_frame = ttk.LabelFrame(main_frame, text="Sample Stage View", padding=10)
        vis_frame.grid(row=0, column=1, rowspan=2, sticky="nsew", padx=5, pady=5)
        self.canvas = tk.Canvas(vis_frame, width=1000, height=1000, bg="white", highlightthickness=1, highlightbackground="#888")
        self.canvas.pack(fill=tk.BOTH, expand=True)

        main_frame.columnconfigure(0, weight=0)
        main_frame.columnconfigure(1, weight=1)
        main_frame.rowconfigure(0, weight=1)
        main_frame.rowconfigure(1, weight=0)

        style = ttk.Style()
        style.configure("TButton", font=("Segoe UI", 10))
        style.configure("TLabel", font=("Segoe UI", 0))
        style.configure("TLabelframe.Label", font=("Segoe UI", 15, "bold", "underline"))

    def __draw_stage(self):
        self.canvas.delete("all")
        platform_color = "lightgray" if self.__ctl.is_enabled() else "darkgray"

        self.center_x = self.canvas.winfo_width() // 2
        self.center_y = self.canvas.winfo_height() // 2

        if not self.__ctl.is_enabled():
            if self.__ctl.get_state() != stage_client.STATE_MOVING:
                self.canvas.create_text(self.center_x, self.center_y + 300, text="Actuators disabled", fill="gray", angle=0, font=("Helvetica", 56, "bold"))
            else:
                self.canvas.create_text(self.center_x, self.center_y + 300, text="Actuators starting", fill="gray", angle=0, font=("Helvetica", 56, "bold"))
        elif self.__ctl.is_at_limit():
            platform_color = "yellow"
            self.canvas.create_text(self.center_x, self.center_y + 300, text="Lin actuator at limit!", fill="red", angle=0, font=("Helvetica", 56, "bold"))
        elif self.__ctl.get_state() == stage_client.STATE_HOMING:
            platform_color = "yellow"
            self.canvas.create_text(self.center_x + 500, self.center_y + 300, text="Homing process in progress", fill="red", angle=0, font=("Helvetica", 56, "bold"))

        exposure_x = self.center_x + stage_client.EXPOSURE_OFFSET_Z * self.vis_scale
        exposure_y = self.center_y - stage_client.EXPOSURE_OFFSET_X * self.vis_scale
        
        draw_sample_stage(
            self.canvas,
            self.center_x, 
            self.center_y, 
            self.vis_scale, 
            self.current_linear_pos, 
            self.current_angle, 
            self.samples, 
            RING_RADII, 
            exposure_x, 
            exposure_y, 
            platform_color)
        
        self.canvas.update_idletasks()

    def update_gui_position(self):
        self.position_label.config(text=f"ROT: {self.current_angle:.2f} deg, LIN: {self.current_linear_pos:.2f} mm")
        self.__draw_stage()

    def update_status(self, text):
        self.status_label.config(text=text)

    def handle_window(self):
        if self.__own_window and hasattr(self.root, "protocol"):
            self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def on_close(self):
        self.cleanup()
        if self.__own_window and hasattr(self.root, "destroy"):
            self.root.destroy()

    def cleanup(self):
        self.__close_progress_dialog()
        self.__ctl.close()



def main(_args: argparse.Namespace):
    root = tk.Tk()
    ctl = SampleMotionDDSClient()
    app = SampleStageControl(root, ctl)

    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass
    finally:
        app.cleanup()

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sample motion DDS GUI")
    args = parser.parse_args()
    main(args)
