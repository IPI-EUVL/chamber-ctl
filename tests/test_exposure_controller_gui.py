from ipi_ecs.dds import client

import chamber_ctl.gui.exposure_controller as exposure_controller_gui
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


class _CompletedEvent:
    def is_in_progress(self):
        return False

    def get_state(self, _subsystem_uuid):
        return client.EVENT_REJ

    def get_result(self, _subsystem_uuid):
        return b"The digitizer spool has no unreleased capture session."


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


def test_failed_acquisition_recovery_reports_its_event_result(monkeypatch) -> None:
    gui = object.__new__(ExposureControllerGUI)
    status = _Label()
    errors = []
    gui._ExposureControllerGUI__acquisition_control_handle = _CompletedEvent()
    gui._ExposureControllerGUI__acquisition_control_name = "Orphan recovery requested"
    gui._ExposureControllerGUI__status_acquisition_value = status
    gui._ExposureControllerGUI__root = object()
    monkeypatch.setattr(
        exposure_controller_gui.messagebox,
        "showerror",
        lambda _title, message, **_kwargs: errors.append(message),
    )

    gui._ExposureControllerGUI__update_acquisition_control_result()

    assert status.text == "The digitizer spool has no unreleased capture session."
    assert errors == ["The digitizer spool has no unreleased capture session."]
    assert gui._ExposureControllerGUI__acquisition_control_handle is None