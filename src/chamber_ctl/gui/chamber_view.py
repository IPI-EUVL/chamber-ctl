import tkinter as tk
from tkinter import ttk

from chamber_ctl.gui.sample_motion_gui import rotated_rectangle_coords, draw_sample_platform 

def draw_chamber(canvas, chopper_angle_rad, sample_stage_l, sample_stage_r, laser_active, target_l):
    