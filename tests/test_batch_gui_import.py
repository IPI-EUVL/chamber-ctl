from chamber_ctl.gui.batch_controller import BatchControllerClient, BatchControllerGUI


def test_batch_gui_types_import_without_startup_side_effects() -> None:
    assert BatchControllerClient.__name__ == "BatchControllerClient"
    assert BatchControllerGUI.__name__ == "BatchControllerGUI"