import json
from chamber_ctl.subsystems.exposure_controller import ExposureSettings


def test_new_exposure_settings_default_to_nominal_chopper_frequency_and_unselected_calibration() -> None:
    settings = ExposureSettings()

    assert settings.get_chopper_frequency_hz() == 192.0
    assert settings.get_calibration_profile_id() == ""
    assert settings.get_calibration_revision() == 0
    assert "source_calibrations" not in settings.get_dict()


def test_historical_settings_decode_with_unknown_frequency_and_unselected_calibration() -> None:
    current = ExposureSettings(target_dose=1.0)
    historical = current.get_dict()
    historical.pop("calibration_profile_id")
    historical.pop("calibration_revision")
    historical.pop("chopper_frequency_hz")
    historical["source_calibrations"] = [
        {
            "source_kind": "siglent",
            "source_id": "legacy",
            "profile_id": "11111111-1111-1111-1111-111111111111",
            "revision": 1,
        }
    ]

    decoded = ExposureSettings.decode(json.dumps(historical))

    assert decoded.get_chopper_frequency_hz() is None
    assert decoded.get_calibration_profile_id() == ""
    assert decoded.get_calibration_revision() == 0
    assert "source_calibrations" not in decoded.get_dict()


def test_exposure_settings_are_scalar_tag_values() -> None:
    assert all(
        value is None or isinstance(value, (str, int, float))
        for value in ExposureSettings().get_dict().values()
    )
