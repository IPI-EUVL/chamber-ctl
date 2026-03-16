import tkinter as tk
from tkinter import ttk
from tkinter import messagebox
import math
import threading
import queue

from chamber_ctl.interfaces import target_controller_interface
from chamber_ctl.subsystems.uuids import UUID_TARGET_CONTROLLER
from ipi_ecs.cli.captive_cli import wait_for_event
import ipi_ecs.dds.magics as magics

LIN_LENGTH = 67.5

def rotated_rectangle_coords(cx, cy, width, height, angle_deg):
    """
    Calculate the coordinates of a rotated rectangle.
    cx, cy: center of rectangle
    width, height: dimensions of rectangle
    angle_deg: rotation angle in degrees (counterclockwise)
    """
    angle_rad = math.radians(angle_deg)
    cos_a = math.cos(angle_rad)
    sin_a = math.sin(angle_rad)

    # Half dimensions
    hw, hh = width / 2, height / 2

    # Rectangle corners relative to center (before rotation)
    corners = [
        (-hw, -hh),  # top-left
        ( hw, -hh),  # top-right
        ( hw,  hh),  # bottom-right
        (-hw,  hh)   # bottom-left
    ]

    # Apply rotation to each corner
    rotated = []
    for x, y in corners:
        xr = cx + x * cos_a - y * sin_a
        yr = cy + x * sin_a + y * cos_a
        rotated.extend((xr, yr))

    return rotated

class TargetControlGUI:
    def __init__(self, root, itf : target_controller_interface.TargetClient):
        self.root = root
        self.root.title("Target Motion Control GUI")
        self.root.geometry("1250x760")

        self.__run = True
        self.__ui_queue = queue.Queue()
        self.__cmd_queue = queue.Queue()

        self.__itf = itf

        self.__initialize_component()
        self.handle_window()

        self.__worker = threading.Thread(target=self.__worker_thread, daemon=True)
        self.__worker.start()

        self.root.after(100, self.__ui_tick)
        self.root.after(200, self.__refresh_view)

    def __initialize_component(self):
        main_frame = ttk.Frame(self.root, padding=12)
        main_frame.pack(fill=tk.BOTH, expand=True)

        left = ttk.Frame(main_frame)
        left.pack(side=tk.LEFT, fill=tk.Y)

        right = ttk.Frame(main_frame)
        right.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=(12, 0))

        status_frame = tk.LabelFrame(left, text="Status", padx=10, pady=8)
        status_frame.pack(fill=tk.X)

        self.__connection_label = tk.Label(status_frame, text="Connection: DISCONNECTED", font=("Arial", 11))
        self.__connection_label.pack(anchor="w")

        self.__mode_label = tk.Label(status_frame, text="Mode: Unknown", font=("Arial", 11))
        self.__mode_label.pack(anchor="w")

        self.__position_label = tk.Label(status_frame, text="Position: L=N/A R=N/A", font=("Arial", 11))
        self.__position_label.pack(anchor="w")

        self.__target_label = tk.Label(status_frame, text="Target: L=N/A R=N/A", font=("Arial", 11))
        self.__target_label.pack(anchor="w")

        self.__time_label = tk.Label(status_frame, text="Time: N/A", font=("Arial", 11))
        self.__time_label.pack(anchor="w")

        self.__offset_label = tk.Label(status_frame, text="Offset: L=0.000 R=0.000", font=("Arial", 11))
        self.__offset_label.pack(anchor="w")

        button_frame = tk.LabelFrame(left, text="Control", padx=10, pady=8)
        button_frame.pack(fill=tk.X, pady=(10, 0))

        tk.Button(button_frame, text="Set Offset Here", width=12, command=self.__on_set_offset_here).grid(row=0, column=0, padx=4, pady=4)
        tk.Button(button_frame, text="Clear Offset", width=12, command=self.__on_clear_offset).grid(row=0, column=1, padx=4, pady=4)
        tk.Button(button_frame, text="Set Start", width=12, command=self.__on_set_start).grid(row=1, column=0, padx=4, pady=4)
        tk.Button(button_frame, text="Home", width=12, command=self.__on_home).grid(row=1, column=1, padx=4, pady=4)
        tk.Button(button_frame, text="Start", width=12, command=self.__on_start).grid(row=2, column=0, padx=4, pady=4)
        tk.Button(button_frame, text="Stop", width=12, command=self.__on_stop).grid(row=2, column=1, padx=4, pady=4)

        set_time_frame = tk.LabelFrame(left, text="Set Time Position", padx=10, pady=8)
        set_time_frame.pack(fill=tk.X, pady=(10, 0))

        tk.Label(set_time_frame, text="Time (s)", font=("Arial", 10)).grid(row=0, column=0, padx=4, pady=4, sticky="w")
        self.__time_entry = tk.Entry(set_time_frame, width=12)
        self.__time_entry.insert(0, "0.0")
        self.__time_entry.grid(row=0, column=1, padx=4, pady=4, sticky="w")
        tk.Button(set_time_frame, text="Set", width=10, command=self.__on_set_time).grid(row=0, column=2, padx=4, pady=4)

        goto_frame = tk.LabelFrame(left, text="Go To Position", padx=10, pady=8)
        goto_frame.pack(fill=tk.X, pady=(10, 0))

        tk.Label(goto_frame, text="L (mm)", font=("Arial", 10)).grid(row=0, column=0, padx=4, pady=4, sticky="w")
        self.__goto_l_entry = tk.Entry(goto_frame, width=10)
        self.__goto_l_entry.insert(0, "0.0")
        self.__goto_l_entry.grid(row=0, column=1, padx=4, pady=4)

        tk.Label(goto_frame, text="R (rad)", font=("Arial", 10)).grid(row=0, column=2, padx=4, pady=4, sticky="w")
        self.__goto_r_entry = tk.Entry(goto_frame, width=10)
        self.__goto_r_entry.insert(0, "0.0")
        self.__goto_r_entry.grid(row=0, column=3, padx=4, pady=4)

        tk.Label(goto_frame, text="L speed", font=("Arial", 10)).grid(row=1, column=0, padx=4, pady=4, sticky="w")
        self.__goto_l_speed_entry = tk.Entry(goto_frame, width=10)
        self.__goto_l_speed_entry.insert(0, "2.0")
        self.__goto_l_speed_entry.grid(row=1, column=1, padx=4, pady=4)

        tk.Label(goto_frame, text="R speed", font=("Arial", 10)).grid(row=1, column=2, padx=4, pady=4, sticky="w")
        self.__goto_r_speed_entry = tk.Entry(goto_frame, width=10)
        self.__goto_r_speed_entry.insert(0, "1.0")
        self.__goto_r_speed_entry.grid(row=1, column=3, padx=4, pady=4)

        tk.Button(goto_frame, text="Go To", width=12, command=self.__on_goto).grid(row=2, column=0, columnspan=4, padx=4, pady=(8, 4))

        jog_frame = tk.LabelFrame(left, text="Jog", padx=10, pady=8)
        jog_frame.pack(fill=tk.X, pady=(10, 0))

        tk.Label(jog_frame, text="L jog", font=("Arial", 10)).grid(row=0, column=0, padx=4, pady=4, sticky="w")
        self.__jog_l_entry = tk.Entry(jog_frame, width=10)
        self.__jog_l_entry.insert(0, "0.0")
        self.__jog_l_entry.grid(row=0, column=1, padx=4, pady=4)

        tk.Label(jog_frame, text="R jog", font=("Arial", 10)).grid(row=0, column=2, padx=4, pady=4, sticky="w")
        self.__jog_r_entry = tk.Entry(jog_frame, width=10)
        self.__jog_r_entry.insert(0, "0.0")
        self.__jog_r_entry.grid(row=0, column=3, padx=4, pady=4)

        tk.Button(jog_frame, text="Set Jog", width=12, command=self.__on_set_jog).grid(row=1, column=0, columnspan=2, padx=4, pady=(8, 4))
        tk.Button(jog_frame, text="Stop Jog", width=12, command=self.__on_stop_jog).grid(row=1, column=2, columnspan=2, padx=4, pady=(8, 4))

        result_frame = tk.LabelFrame(left, text="Result", padx=10, pady=8)
        result_frame.pack(fill=tk.X, pady=(10, 0))

        self.__result_label = tk.Label(result_frame, text="Last action: none", font=("Arial", 10), justify="left", wraplength=360)
        self.__result_label.pack(anchor="w")

        self.canvas = tk.Canvas(right)
        self.canvas.pack(fill=tk.BOTH, expand=True)

    def __worker_thread(self):
        while self.__run:
            try:
                cmd, payload = self.__cmd_queue.get(timeout=0.1)
            except queue.Empty:
                continue

            try:
                if cmd == "set_offset":
                    self.__run_event("Set offset", self.__itf.set_offset_here(), 10.0)
                elif cmd == "clear_offset":
                    self.__run_event("Clear offset", self.__itf.clear_offset(), 10.0)
                elif cmd == "set_start":
                    self.__run_event("Set start", self.__itf.set_start_here(), 10.0)
                elif cmd == "home":
                    self.__run_event("Home", self.__itf.home(), 180.0)
                elif cmd == "start":
                    self.__run_event("Start", self.__itf.start_motion(), 10.0)
                elif cmd == "stop":
                    self.__run_event("Stop", self.__itf.stop_motion(), 10.0)
                elif cmd == "set_time":
                    self.__run_event(f"Set time to {payload:.3f}s", self.__itf.set_current_position(payload), 10.0)
                elif cmd == "goto":
                    l_pos, r_pos, l_speed, r_speed = payload
                    ok, msg = self.__itf.goto_position(
                        l_pos=l_pos,
                        r_pos=r_pos,
                        l_speed=l_speed,
                        r_speed=r_speed,
                        timeout=30.0,
                    )
                    if ok:
                        self.__ui_queue.put(("result", f"Go To complete: {msg}"))
                    else:
                        self.__ui_queue.put(("result", f"Go To failed: {msg}"))
                elif cmd == "set_jog":
                    l_jog, r_jog = payload
                    ok = self.__itf.set_jog(l_jog, r_jog)
                    if ok:
                        self.__ui_queue.put(("result", f"Jog set to L={l_jog:.3f}, R={r_jog:.3f}"))
                    else:
                        self.__ui_queue.put(("result", "Failed to set jog."))
                elif cmd == "stop_jog":
                    ok = self.__itf.stop_jog()
                    if ok:
                        self.__ui_queue.put(("result", "Jog stopped."))
                    else:
                        self.__ui_queue.put(("result", "Failed to stop jog."))
            except Exception as exc:
                self.__ui_queue.put(("result", f"Action failed: {exc}"))

    def __run_event(self, label: str, awaiter, timeout: float):
        if awaiter is None:
            self.__ui_queue.put(("result", f"{label} failed: request could not be sent."))
            return

        value, _, reason = wait_for_event(awaiter, UUID_TARGET_CONTROLLER, timeout)
        if value is None:
            self.__ui_queue.put(("result", f"{label} failed: {reason}"))
            return

        if isinstance(value, bytes) and value.startswith(magics.OP_OK):
            text = value.decode("utf-8", errors="replace")
            self.__ui_queue.put(("result", f"{label} complete: {text}"))
            return

        text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)
        self.__ui_queue.put(("result", f"{label} failed: {text} ({reason})"))

    def __ui_tick(self):
        while not self.__ui_queue.empty():
            msg, payload = self.__ui_queue.get()
            if msg == "result":
                self.__result_label.config(text=f"Last action: {payload}")

        if self.__run:
            self.root.after(100, self.__ui_tick)

    def __refresh_view(self):
        connected = self.__itf.is_connected()
        self.__connection_label.config(text=f"Connection: {'CONNECTED' if connected else 'DISCONNECTED'}")

        state = self.__itf.get_state()

        if state is None:
            self.__mode_label.config(text="Mode: Unknown")
            self.__position_label.config(text="Position: L=N/A R=N/A")
            self.__target_label.config(text="Target: L=N/A R=N/A")
            self.__time_label.config(text="Time: N/A")
            self.__offset_label.config(text="Offset: L=N/A R=N/A")
        else:
            mode = []
            if state.is_running:
                mode.append("RUNNING")
            if state.is_jogging:
                mode.append("JOGGING")
            if state.is_homing:
                mode.append("HOMING")
            if not mode:
                mode.append("IDLE")

            self.__mode_label.config(text=f"Mode: {' | '.join(mode)}")
            self.__position_label.config(text=f"Position: L={state.position[0]:.3f} R={state.position[1]:.3f}")
            self.__target_label.config(text=f"Target: L={state.target_position[0]:.3f} R={state.target_position[1]:.3f}")
            self.__time_label.config(text=f"Time: t={state.current_time:.3f}s seg={state.current_segment}")
            offset_pos = state.offset_position if hasattr(state, "offset_position") else (0.0, 0.0)
            self.__offset_label.config(text=f"Offset: L={offset_pos[0]:.3f} R={offset_pos[1]:.3f}")

        self.__draw_stage()

        if self.__run:
            self.root.after(200, self.__refresh_view)

    def __on_set_offset_here(self):
        self.__cmd_queue.put(("set_offset", None))

    def __on_clear_offset(self):
        self.__cmd_queue.put(("clear_offset", None))

    def __on_set_start(self):
        if not messagebox.askyesno("Confirm Set Start", "Set START position to current position?"):
            return
        self.__cmd_queue.put(("set_start", None))

    def __on_home(self):
        self.__cmd_queue.put(("home", None))

    def __on_start(self):
        self.__cmd_queue.put(("start", None))

    def __on_stop(self):
        self.__cmd_queue.put(("stop", None))

    def __on_set_time(self):
        try:
            position = float(self.__time_entry.get().strip())
        except ValueError:
            self.__result_label.config(text="Last action: invalid time value")
            return

        self.__cmd_queue.put(("set_time", position))

    def __on_goto(self):
        try:
            l_pos = float(self.__goto_l_entry.get().strip())
            r_pos = float(self.__goto_r_entry.get().strip())
            l_speed = float(self.__goto_l_speed_entry.get().strip())
            r_speed = float(self.__goto_r_speed_entry.get().strip())
        except ValueError:
            self.__result_label.config(text="Last action: invalid Go To input")
            return

        self.__cmd_queue.put(("goto", (l_pos, r_pos, l_speed, r_speed)))

    def __on_set_jog(self):
        try:
            l_jog = float(self.__jog_l_entry.get().strip())
            r_jog = float(self.__jog_r_entry.get().strip())
        except ValueError:
            self.__result_label.config(text="Last action: invalid jog input")
            return

        self.__cmd_queue.put(("set_jog", (l_jog, r_jog)))

    def __on_stop_jog(self):
        self.__cmd_queue.put(("stop_jog", None))

    def __pos_to_pixels(self, l, r):
        x = self.center_x + -l * self.vis_scale_l
        y = self.center_y + (r - 3.14) * self.vis_scale_r
        return x + LIN_LENGTH * self.vis_scale_l, y

    def __draw_line_from_to_mm(self, start_l, start_r, end_l, end_r, color="blue"):
        # L is inverted

        end_r -= math.floor(start_r / 6.28) * 6.28
        start_r -= math.floor(start_r / 6.28) * 6.28

        if end_r > 6.28:
            self.__draw_line_from_to_mm(start_l, start_r, 0.0, end_r - 6.28)
            end_r = 6.28
        
        start_x, start_y = self.__pos_to_pixels(start_l, start_r)
        end_x, end_y = self.__pos_to_pixels(end_l, end_r)

        self.canvas.create_line(start_x, start_y, end_x, end_y, fill=color, width=2)

    def __draw_stage(self):
        self.canvas.delete("all")
                
        self.vis_scale_l = 10.0
        self.vis_scale_r = -20.0

        self.center_x = self.canvas.winfo_width() / 2 - (LIN_LENGTH * self.vis_scale_l) / 2
        self.center_y = self.canvas.winfo_height() / 2 + (6.28 * self.vis_scale_r) / 2

        profile = self.__itf.get_profile()
        state = self.__itf.get_state()

        if profile is None:
            self.canvas.update_idletasks()
            return
        
        bound_l, bound_r = profile.get_bounds()
        self.center_x -= bound_l / 2 * self.vis_scale_l
        self.center_y -= bound_r / 2 * self.vis_scale_r

        points = rotated_rectangle_coords(
            self.center_x + LIN_LENGTH * self.vis_scale_l / 2,
            self.center_y,
            LIN_LENGTH * self.vis_scale_l,
            math.pi * 2 * self.vis_scale_r,
            0
        )

        points.append([self.center_x - 20, self.center_y])

        self.canvas.create_polygon(
            points,
            outline="black",
            fill="lightgray"
        )

        segments = profile.get_segments()

        if segments is None or len(segments) == 0:
            self.canvas.update_idletasks()
            return
        
        off_l = 0
        off_r = 0

        l_offset = 0.0
        r_offset = 0.0
        if state is not None:
            if hasattr(state, "offset_position"):
                l_offset, r_offset = state.offset_position

        cur_time = 0.0

        while off_l < LIN_LENGTH:
            c_l = 0
            c_r = 0

            for segment in segments:
                l_t = abs((segment.lin_target - c_l) / segment.lin_velocity) if segment.lin_velocity > 0 else 0
                r_t = abs((segment.rot_target - c_r) / segment.rot_velocity) if segment.rot_velocity > 0 else 0

                cur_time += max(l_t, r_t)

                color = "blue"
                if state is not None and cur_time > state.current_time:
                    color = "red"

                self.__draw_line_from_to_mm(
                    start_l=(c_l + off_l + l_offset),
                    start_r=(c_r + off_r + r_offset),
                    end_l=(segment.lin_target + off_l + l_offset),
                    end_r=(segment.rot_target + off_r + r_offset),
                    color=color
                )

                c_l = segment.lin_target
                c_r = segment.rot_target

            off_l += c_l
            off_r += c_r

        if state is not None:
            l_p, r_p = profile.get_position_at_time(state.current_time % profile.get_length())
            l_off, r_off = profile.get_end_position()
            l_p += l_off * math.floor(state.current_time / profile.get_length())
            r_p += r_off * math.floor(state.current_time / profile.get_length())

            l_cur, r_cur = state.position

            if hasattr(state, "offset_position"):
                l_p += state.offset_position[0]
                r_p += state.offset_position[1]

            l_cur -= state.start_position[0]
            r_cur -= state.start_position[1]

            l_p, r_p = l_p % LIN_LENGTH, r_p % 6.28
            l_cur, r_cur = l_cur % LIN_LENGTH, r_cur % 6.28

            self.canvas.create_oval(
                self.__pos_to_pixels(l_p, r_p)[0] - 5,
                self.__pos_to_pixels(l_p, r_p)[1] - 5,
                self.__pos_to_pixels(l_p, r_p)[0] + 5,
                self.__pos_to_pixels(l_p, r_p)[1] + 5,
                fill="green"
            )

            self.canvas.create_oval(
                self.__pos_to_pixels(l_cur, r_cur)[0] - 5,
                self.__pos_to_pixels(l_cur, r_cur)[1] - 5,
                self.__pos_to_pixels(l_cur, r_cur)[0] + 5,
                self.__pos_to_pixels(l_cur, r_cur)[1] + 5,
                fill="red"
            )
        self.canvas.update_idletasks()

    def handle_window(self):
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def on_close(self):
        self.__run = False
        self.root.destroy()

if __name__ == "__main__":
    itf = target_controller_interface.TargetClient()

    root = tk.Tk()
    app = TargetControlGUI(root, itf)
    root.mainloop()

    itf.close()