import uuid

import chamber_ctl.gui.batch_controller as batch_controller_gui
from chamber_ctl.data.calibration import SourceCalibrationBinding, SourceConfiguration, SourceKey
from chamber_ctl.gui.batch_controller import BatchControllerClient, BatchControllerGUI
from chamber_ctl.subsystems.batcher import ExposureTemplate


class _Value:
    def __init__(self, value):
        self.value = value

    def set(self, value):
        self.value = value


def test_batch_gui_types_import_without_startup_side_effects() -> None:
    assert BatchControllerClient.__name__ == "BatchControllerClient"
    assert BatchControllerGUI.__name__ == "BatchControllerGUI"


def test_source_calibration_editor_updates_batch_template(monkeypatch) -> None:
    binding = SourceCalibrationBinding("siglent", "scope-1", uuid.uuid4(), 2)
    gui = object.__new__(BatchControllerGUI)
    gui._root = object()
    gui._data_path = "datasets"
    gui._source_options_provider = lambda: (SourceKey("siglent", "scope-1"),)
    gui._template_value = ExposureTemplate("Batch")
    gui._source_calibration_options = {}
    gui._source_calibration_text = _Value("None")
    call = {}

    def edit(*_args, **kwargs):
        call.update(kwargs)
        return SourceConfiguration((binding,), binding.source_key)

    monkeypatch.setattr(batch_controller_gui, "edit_source_calibrations", edit)

    gui._edit_source_calibrations()

    assert gui._template_value.source_calibrations == (binding,)
    assert gui._template_value.primary_source == binding.source_key
    assert call["source_options"] == (SourceKey("siglent", "scope-1"),)
    assert call["data_path"] == "datasets"
