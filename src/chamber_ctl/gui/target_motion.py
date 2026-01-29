import tkinter as tk
from tkinter import ttk
from labjack import ljm
import time
import math
import threading
from math import cos, sin, radians
from queue import Queue

from chamber_ctl.interfaces import target_controller_interface
from chamber_ctl.subsystems.target_controller import TargetMotionControllerState, MotionState, TargetMotionProfile, MotionSegment, TargetMotionConfig

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
        root.title("Target Motion Control GUI")

        self.__itf = itf
        
        #GUI setup 
        self.__initialize_component()
        self.handle_window()
        self.__draw_stage()

        #threading.Thread(target=self.__update_thread, daemon=True).start()
        
        #Queue processer
        self.gui_q_processing()

    def gui_q_processing(self):
        self.__draw_stage()
            
        self.root.after(50, self.gui_q_processing)

    def __initialize_component(self):
        main_frame = ttk.Frame(self.root, padding=10)
        main_frame.pack(fill=tk.BOTH, expand=True)


        self.canvas = tk.Canvas(main_frame)
        self.canvas.pack(fill=tk.BOTH, expand=True)

    def __draw_stage(self):
        self.canvas.delete("all")
        self.center_x = self.canvas.winfo_width() / 2
        self.center_y = self.canvas.winfo_height() / 2
        
        self.vis_scale_l = 10.0
        self.vis_scale_r = -20.0

        profile = self.__itf.get_profile()

        if profile is None:
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
            return
        
        off_l = 0
        off_r = 0

        while off_l < LIN_LENGTH and off_r < math.pi * 2:
            c_l = 0
            c_r = 0

            for segment in segments:
                start_x = self.center_x + (c_l + off_l) * self.vis_scale_l
                start_y = self.center_y + (c_r + off_r) * self.vis_scale_r

                end_x = self.center_x + (segment.lin_target + off_l) * self.vis_scale_l
                end_y = self.center_y + (segment.rot_target + off_r) * self.vis_scale_r
                c_l = segment.lin_target
                c_r = segment.rot_target

                self.canvas.create_line(start_x, start_y, end_x, end_y, fill="blue", width=2)

            off_l += c_l
            off_r += c_r

        self.canvas.update_idletasks()

    def handle_window(self):
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def on_close(self):
        self.root.destroy()

if __name__ == "__main__":
    itf = target_controller_interface.TargetClient()

    root = tk.Tk()
    app = TargetControlGUI(root, itf)
    root.mainloop()

    itf.close()