import json
import multiprocessing
import os
import time

from ipi_ecs.subsystems.experiment_controller import ExperimentController, RunSettings
from chamber_ctl.subsystems import uuids

class ExposureSettings(RunSettings):
    def __init__(self, target_time: float = 0.0, target_dose: float = 0.0, operator: str = "", zr_filter: str = "", sample: str = "", sample_type: str = "", base_pressure: float = 0.0, operating_pressure: float = 0.0, flow_sccm: float = 0.0, calibration_profile_id: str = "", calibration_revision: int = 0, chopper_frequency_hz: float | None = 192.0):
        super().__init__()
        self.data = {
            "name": "",
            "description": "",
        }
        self.data["target_time"] = target_time
        self.data["target_dose"] = target_dose
        self.data["operator"] = operator
        self.data["zr_filter"] = zr_filter
        self.data["sample"] = sample
        self.data["sample_type"] = sample_type
        self.data["base_pressure"] = base_pressure
        self.data["operating_pressure"] = operating_pressure
        self.data["flow_sccm"] = flow_sccm
        self.data["calibration_profile_id"] = calibration_profile_id
        self.data["calibration_revision"] = calibration_revision
        self.data["chopper_frequency_hz"] = chopper_frequency_hz

    def set_attr(self, key, value):
        if (self.data["target_time"] is not None and key == "target_dose" and float(value) > 0.1) and self.data["target_time"] >= 0.1:
            raise ValueError("Cannot set target_dose when target_time is already set. Please clear target_time before setting target_dose.")
        if (self.data["target_dose"] is not None and self.data["target_dose"] >= 0.1) and key == "target_time" and float(value) > 0.1:
            raise ValueError("Cannot set target_time when target_dose is already set. Please clear target_dose before setting target_time.")
        assert key in self.data, f"Invalid key '{key}' for ExposureSettings"


        #assert type(value) == self.data.get(key).__class__, f"Type mismatch for {key}: expected {self.data.get(key).__class__}, got {type(value)}"
        return super().set_attr(key, value)

    def get_target_time(self) -> float:
        return self.data.get("target_time", 0.0)
    
    def get_base_pressure(self) -> float:
        return self.data.get("base_pressure", 0.0)

    def get_operating_pressure(self) -> float:
        return self.data.get("operating_pressure", 0.0)

    def get_flow_sccm(self) -> float:
        return self.data.get("flow_sccm", 0.0)

    def get_operator(self) -> str:
        return self.data.get("operator", "")
    
    def get_zr_filter(self) -> str:
        return self.data.get("zr_filter", "")
    
    def get_sample(self) -> str:
        return self.data.get("sample", "")
    
    def get_sample_type(self) -> str:
        return self.data.get("sample_type", "")
    
    def get_target_dose(self) -> float:
        return self.data.get("target_dose", 0.0)

    def get_calibration_profile_id(self) -> str:
        return self.data.get("calibration_profile_id", "")

    def get_calibration_revision(self) -> int:
        return self.data.get("calibration_revision", 0)

    def get_chopper_frequency_hz(self) -> float | None:
        return self.data.get("chopper_frequency_hz")
    
    @staticmethod
    def decode(data: str) -> "ExposureSettings":
        data_dict = json.loads(data)
        obj = ExposureSettings()
        obj.data = data_dict

        obj.data.setdefault("calibration_profile_id", "")
        obj.data.setdefault("calibration_revision", 0)
        obj.data.setdefault("chopper_frequency_hz", None)

        assert obj.data.get("target_time") is not None, "Decoded ExposureSettings missing target_time"
        assert obj.data.get("target_dose") is not None, "Decoded ExposureSettings missing target_dose"
        assert obj.data.get("operator") is not None, "Decoded ExposureSettings missing operator"
        assert obj.data.get("zr_filter") is not None, "Decoded ExposureSettings missing zr_filter"
        assert obj.data.get("sample") is not None, "Decoded ExposureSettings missing sample"
        assert obj.data.get("sample_type") is not None, "Decoded ExposureSettings missing sample_type"
        assert obj.data.get("base_pressure") is not None, "Decoded ExposureSettings missing base_pressure"
        assert obj.data.get("operating_pressure") is not None, "Decoded ExposureSettings missing operating_pressure"
        assert obj.data.get("flow_sccm") is not None, "Decoded ExposureSettings missing flow_sccm"

        return obj

def main(stop_event: "multiprocessing.Event"):
    __SAVE_PATH = os.path.join(os.environ["EUVL_PATH"], "datasets")
    m_run_controller = ExperimentController("Exposure Controller", uuids.UUID_EXPOSURE_CONTROLLER, "exposure", __SAVE_PATH)
    m_run_controller.add_required_subsystem(uuids.UUID_TARGET_CONTROLLER, "Target Controller")
    m_run_controller.add_required_subsystem(uuids.UUID_EUV_ACQUISITION_CONTROLLER, "EUV Acquisition Controller")
    m_run_controller.add_required_subsystem(uuids.UUID_LASER_CONTROLLER, "Laser Controller")
    m_run_controller.add_required_subsystem(uuids.UUID_SAMPLE_MOTION_CONTROLLER, "Sample Motion Controller")
    m_run_controller.add_expected_run_event_stream(
        uuids.UUID_EUV_ACQUISITION_CONTROLLER,
        "acquisition.timing",
    )
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