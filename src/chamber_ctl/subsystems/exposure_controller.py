import json
import multiprocessing
import os
import pickle
import queue
import time
import uuid
import mt_events
import segment_bytes

from enum import Enum

from ipi_ecs.core import daemon
import ipi_ecs.dds.subsystem as subsystem
import ipi_ecs.dds.types as types
import ipi_ecs.dds.client as client
import ipi_ecs.dds.magics as magics
import ipi_ecs.core.tcp as tcp
from ipi_ecs.dds.magics import *

from ipi_ecs.logging.client import LogClient
from ipi_ecs.db.db_library import Library

from ipi_ecs.subsystems.experiment_controller import ExperimentController, RunSettings
from chamber_ctl.subsystems import uuids

class ExposureSettings(RunSettings):
    def __init__(self, target_time: float = 0.0, target_dose: float = 0.0, operator: str = "", zr_filter: str = "", sample: str = ""):
        super().__init__()
        self.data["target_time"] = target_time
        self.data["target_dose"] = target_dose
        self.data["operator"] = operator
        self.data["zr_filter"] = zr_filter
        self.data["sample"] = sample

    def set_attr(self, key, value):
        if (self.data["target_time"] is not None and key == "target_dose" and float(value) > 0.1) and self.data["target_time"] >= 0.1:
            raise ValueError("Cannot set target_dose when target_time is already set. Please clear target_time before setting target_dose.")
        if (self.data["target_dose"] is not None and self.data["target_dose"] >= 0.1) and key == "target_time" and float(value) > 0.1:
            raise ValueError("Cannot set target_time when target_dose is already set. Please clear target_dose before setting target_time.")
        return super().set_attr(key, value)

    def get_target_time(self) -> float:
        return self.data.get("target_time", 0.0)
    
    def get_operator(self) -> str:
        return self.data.get("operator", "")
    
    def get_zr_filter(self) -> str:
        return self.data.get("zr_filter", "")
    
    def get_sample(self) -> str:
        return self.data.get("sample", "")
    
    def get_target_dose(self) -> float:
        return self.data.get("target_dose", 0.0)
    
    @staticmethod
    def decode(data: str) -> "ExposureSettings":
        data_dict = json.loads(data)
        obj = ExposureSettings()
        obj.data = data_dict

        assert obj.data.get("target_time") is not None, "Decoded ExposureSettings missing target_time"
        assert obj.data.get("target_dose") is not None, "Decoded ExposureSettings missing target_dose"
        assert obj.data.get("operator") is not None, "Decoded ExposureSettings missing operator"
        assert obj.data.get("zr_filter") is not None, "Decoded ExposureSettings missing zr_filter"
        assert obj.data.get("sample") is not None, "Decoded ExposureSettings missing sample"
        
        return obj

def main(stop_event: "multiprocessing.Event"):
    __SAVE_PATH = os.path.join(os.environ["EUVL_PATH"], "datasets")
    m_run_controller = ExperimentController("ExposureController", uuids.UUID_EXPOSURE_CONTROLLER, "exposure", __SAVE_PATH)
    m_run_controller.add_required_subsystem(uuids.UUID_TARGET_CONTROLLER, "Target Controller")
    m_run_controller.add_required_subsystem(uuids.UUID_OSCILLOSCOPE_CONTROLLER, "Oscilloscope Controller")
    m_run_controller.add_required_subsystem(uuids.UUID_LASER_CONTROLLER, "Laser Controller")
    m_run_controller.register_experiment_settings_type(ExposureSettings)

    try:
        while m_run_controller.ok() and not (stop_event is not None and stop_event.is_set()):
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        m_run_controller.close()

if __name__ == "__main__":
    main(None)