import uuid

from chamber_ctl.data.calibration import SourceCalibrationBinding, SourceKey
from chamber_ctl.gui.calibration_editor import calibration_profile_from_fields
from chamber_ctl.gui.central import configured_sources_from_status_rows
from chamber_ctl.gui.source_calibration_editor import (
    available_source_keys,
    source_calibration_from_selection,
    source_calibration_summary,
)


class _StatusItem:
    def __init__(self, code: int, message: str) -> None:
        self._code = code
        self._message = message

    def get_code(self) -> int:
        return self._code

    def get_message(self) -> str:
        return self._message


def test_editor_selection_builds_source_keyed_binding() -> None:
    profile_id = uuid.uuid4()

    binding = source_calibration_from_selection(
        " siglent ",
        " scope-1 ",
        "Scope profile",
        {"Scope profile": (str(profile_id), 3)},
    )

    assert binding == SourceCalibrationBinding("siglent", "scope-1", profile_id, 3)


def test_editor_summary_is_stable_across_binding_order_and_marks_primary() -> None:
    siglent = SourceCalibrationBinding("siglent", "scope-1", uuid.uuid4(), 3)
    integrator = SourceCalibrationBinding("integrator", "pulse-1", uuid.uuid4(), 2)

    assert source_calibration_summary((siglent, integrator), siglent.source_key) == (
        f"integrator/pulse-1: {integrator.profile_id} r2, "
        f"Primary: siglent/scope-1: {siglent.profile_id} r3"
    )


def test_calibration_profile_form_uses_cli_equivalent_defaults() -> None:
    profile_id = uuid.uuid4()

    profile = calibration_profile_from_fields(
        {
            "name": "Scope 1",
            "algorithm_version": "dose-v1-native-integral",
            "signal_polarity": "-1",
            "load_resistance_ohms": "50",
            "photodiode_responsivity_a_per_w": "0.14",
            "illuminated_area_cm2": "0.05",
            "multiplicative_correction": "",
            "additive_pulse_dose_mj_cm2": "",
            "provenance": "Reference measurement",
            "notes": "Fixture",
        },
        profile_id=profile_id,
        created_at=123.0,
    )

    assert profile.profile_id == profile_id
    assert profile.revision == 1
    assert profile.signal_polarity == -1
    assert profile.multiplicative_correction == 1.0
    assert profile.additive_pulse_dose_mj_cm2 == 0.0
    assert profile.created_at == 123.0


def test_source_dropdown_options_come_from_exact_status_items() -> None:
    rows = [
        {
            "connected": True,
            "status_items": [
                _StatusItem(0, "Running"),
                _StatusItem(10, "Configured source: siglent/scope-1"),
            ],
        },
        {
            "connected": False,
            "status_items": [_StatusItem(10, "Configured source: siglent/offline")],
        },
        {
            "connected": True,
            "status_items": [_StatusItem(10, "Unrelated status")],
        },
    ]

    assert configured_sources_from_status_rows(rows) == (SourceKey("siglent", "scope-1"),)


def test_source_dropdown_merges_builtin_discovered_and_bound_sources() -> None:
    binding = SourceCalibrationBinding("integrator", "pulse-1", uuid.uuid4(), 1)

    sources = available_source_keys((binding,), (SourceKey("siglent", "scope-1"),))

    assert sources == (
        SourceKey("integrator", "pulse-1"),
        SourceKey("red_pitaya", "red-pitaya"),
        SourceKey("siglent", "scope-1"),
    )
