import tkinter as tk
from tkinter import ttk
import time
import math
import threading
import uuid
import mt_events
import segment_bytes
from queue import Queue

from chamber_ctl.subsystems import uuids
from chamber_ctl.subsystems.exposure_controller import ExposureSettings
from ipi_ecs.gui.experiment_controller_gui import ExperimentInterface, ExperimentControllerGUI

UUID_EXPOSURE_CONTROLLER = uuid.uuid3(uuid.NAMESPACE_OID, "Exposure Controller")
if __name__ == "__main__":
    itf = ExperimentInterface("exposure", UUID_EXPOSURE_CONTROLLER, exp_settings_type=ExposureSettings)
    
    root = tk.Tk()
    app = ExperimentControllerGUI(root, itf)
    root.mainloop()

    itf.close()