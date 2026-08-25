from chamber_ctl.subsystems.exposure_controller import ExposureSettings


def test_new_exposure_settings_default_to_nominal_chopper_frequency_and_unselected_calibration() -> None:
    settings = ExposureSettings()

    assert settings.get_chopper_frequency_hz() == 192.0
    assert settings.get_calibration_profile_id() == ""
    assert settings.get_calibration_revision() == 0


def test_historical_settings_decode_with_unknown_frequency_and_unselected_calibration() -> None:
    current = ExposureSettings(target_dose=1.0)
    historical = current.get_dict()
    historical.pop("calibration_profile_id")
    historical.pop("calibration_revision")
    historical.pop("chopper_frequency_hz")

    decoded = ExposureSettings.decode(__import__("json").dumps(historical))

    assert decoded.get_chopper_frequency_hz() is None
    assert decoded.get_calibration_profile_id() == ""
    assert decoded.get_calibration_revision() == 0