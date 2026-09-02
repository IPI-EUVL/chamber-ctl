import uuid

from chamber_ctl.data.calibration import SourceCalibrationBinding
from chamber_ctl.gui.source_calibration_editor import (
    source_calibration_from_selection,
    source_calibration_summary,
)


def test_editor_selection_builds_source_keyed_binding() -> None:
    profile_id = uuid.uuid4()

    binding = source_calibration_from_selection(
        " siglent ",
        " scope-1 ",
        "Scope profile",
        {"Scope profile": (str(profile_id), 3)},
    )

    assert binding == SourceCalibrationBinding("siglent", "scope-1", profile_id, 3)


def test_editor_summary_is_stable_across_binding_order() -> None:
    siglent = SourceCalibrationBinding("siglent", "scope-1", uuid.uuid4(), 3)
    integrator = SourceCalibrationBinding("integrator", "pulse-1", uuid.uuid4(), 2)

    assert source_calibration_summary((siglent, integrator)) == (
        f"integrator/pulse-1: {integrator.profile_id} r2, "
        f"siglent/scope-1: {siglent.profile_id} r3"
    )
