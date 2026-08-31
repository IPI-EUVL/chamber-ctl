from chamber_ctl.gui.exposure_controller import ExposureControllerGUI


class _Value:
    def __init__(self, value):
        self.value = value

    def get(self, *_args):
        return self.value


class _Label:
    def __init__(self):
        self.text = None

    def config(self, *, text):
        self.text = text


def test_current_settings_serializes_calibration_revision_as_text() -> None:
    gui = object.__new__(ExposureControllerGUI)
    gui._ExposureControllerGUI__name_input = _Value("Exposure")
    gui._ExposureControllerGUI__description_input = _Value("Fixture")
    gui._ExposureControllerGUI__target_input = _Value("10")
    gui._ExposureControllerGUI__target_type_var = _Value("dose")
    gui._ExposureControllerGUI__operator_combo = _Value("Operator")
    gui._ExposureControllerGUI__zr_filter_combo = _Value("Zr")
    gui._ExposureControllerGUI__sample_combo = _Value("1")
    gui._ExposureControllerGUI__sample_type_combo = _Value("Sample")
    gui._ExposureControllerGUI__base_pressure_input = _Value("0")
    gui._ExposureControllerGUI__operating_pressure_input = _Value("0")
    gui._ExposureControllerGUI__flowrate_input = _Value("0")
    gui._ExposureControllerGUI__chopper_frequency_input = _Value("192")
    gui._ExposureControllerGUI__calibration_options = {"Simulator": ("profile-id", 1)}
    gui._ExposureControllerGUI__calibration_combo = _Value("Simulator")

    settings = gui._ExposureControllerGUI__get_current_settings()

    assert settings["calibration_profile_id"] == "profile-id"
    assert settings["calibration_revision"] == "1"
    assert all(isinstance(value, str) for value in settings.values())


def test_exposure_status_preserves_zero_runtime_from_acquisition_status() -> None:
    gui = object.__new__(ExposureControllerGUI)
    gui._ExposureControllerGUI__acquisition_status_kv = _Value(
        b'{"state":"running","transmitting_runtime_seconds":0.0}'
    )

    status = gui._ExposureControllerGUI__get_acquisition_status()

    assert status["transmitting_runtime_seconds"] == 0.0


def test_exposure_live_labels_fall_back_to_acquisition_status_metrics() -> None:
    gui = object.__new__(ExposureControllerGUI)
    gui._ExposureControllerGUI__acquisition_status_kv = _Value(
        b'{"state":"running","accumulated_dose_mj_cm2":2.5,"transmitting_runtime_seconds":0.0}'
    )
    gui._ExposureControllerGUI__dose_kv = _Value(None)
    gui._ExposureControllerGUI__time_kv = _Value(None)
    gui._ExposureControllerGUI__status_dose_value = _Label()
    gui._ExposureControllerGUI__status_time_value = _Label()
    gui._ExposureControllerGUI__status_acquisition_value = None
    gui._ExposureControllerGUI__status_laser_value = None
    gui._ExposureControllerGUI__status_chopper_value = None
    gui._ExposureControllerGUI__status_chopper_phase_value = None
    gui._ExposureControllerGUI__status_target_value = None
    gui._ExposureControllerGUI__status_target_time_value = None
    gui._ExposureControllerGUI__laser_status_canvas = None
    gui._ExposureControllerGUI__root = None

    gui._ExposureControllerGUI__update_live_status()

    assert gui._ExposureControllerGUI__status_dose_value.text == "2.50 mJ/cm²"
    assert gui._ExposureControllerGUI__status_time_value.text == "0.00 s"