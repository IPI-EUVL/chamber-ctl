import uuid

import chamber_ctl.gui.exposure_controller as exposure_controller_gui
from chamber_ctl.data.calibration import (
    SOURCE_CALIBRATIONS_TAG,
    PRIMARY_SOURCE_TAG,
    SourceConfiguration,
    SourceCalibrationBinding,
    SourceKey,
    decode_primary_source_tag,
    source_calibration_for_source,
)
from chamber_ctl.gui.exposure_controller import ExposureControllerGUI


class _Value:
    def __init__(self, value):
        self.value = value

    def get(self, *_args):
        return self.value

    def set(self, value):
        self.value = value


class _RunTagInterface:
    def __init__(self):
        self.tags = None

    def set_run_tags(self, tags):
        self.tags = tags


class _Label:
    def __init__(self):
        self.text = None

    def config(self, *, text):
        self.text = text


def test_current_settings_leaves_legacy_calibration_fields_unselected() -> None:
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

    settings = gui._ExposureControllerGUI__get_current_settings()

    assert settings["calibration_profile_id"] == ""
    assert settings["calibration_revision"] == "0"
    assert all(isinstance(value, str) for value in settings.values())


def test_source_calibration_editor_updates_direct_run_tags(monkeypatch) -> None:
    binding = SourceCalibrationBinding("siglent", "scope-1", uuid.uuid4(), 2)
    interface = _RunTagInterface()
    gui = object.__new__(ExposureControllerGUI)
    gui._ExposureControllerGUI__settings_locked = False
    gui._ExposureControllerGUI__root = object()
    gui._ExposureControllerGUI__data_path = "datasets"
    gui._ExposureControllerGUI__source_options_provider = lambda: (SourceKey("siglent", "scope-1"),)
    gui._ExposureControllerGUI__acquisition_status_kv = _Value(
        b'{"configured_source_kind":"red_pitaya","configured_source_id":"board-1"}'
    )
    gui._ExposureControllerGUI__source_calibrations = ()
    gui._ExposureControllerGUI__primary_source = None
    gui._ExposureControllerGUI__source_calibration_options = {}
    gui._ExposureControllerGUI__source_calibration_text = _Value("None")
    gui._ExposureControllerGUI__exp_itf = interface
    call = {}

    def edit(*_args, **kwargs):
        call.update(kwargs)
        return SourceConfiguration((binding,), binding.source_key)

    monkeypatch.setattr(exposure_controller_gui, "edit_source_calibrations", edit)

    gui._ExposureControllerGUI__edit_source_calibrations()

    assert gui._ExposureControllerGUI__source_calibrations == (binding,)
    assert source_calibration_for_source(
        interface.tags[SOURCE_CALIBRATIONS_TAG],
        binding.source_key,
    ) == binding
    assert decode_primary_source_tag(interface.tags[PRIMARY_SOURCE_TAG]) == binding.source_key
    assert set(call["source_options"]) == {
        SourceKey("red_pitaya", "red-pitaya"),
        SourceKey("red_pitaya", "board-1"),
        SourceKey("siglent", "scope-1"),
    }
    assert call["data_path"] == "datasets"


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