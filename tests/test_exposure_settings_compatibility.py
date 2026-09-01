import json
import uuid

import pytest

from chamber_ctl.data.calibration import SourceCalibrationBinding
from chamber_ctl.subsystems.exposure_controller import ExposureSettings


def test_new_exposure_settings_default_to_nominal_chopper_frequency_and_unselected_calibration() -> None:
    settings = ExposureSettings()

    assert settings.get_chopper_frequency_hz() == 192.0
    assert settings.get_calibration_profile_id() == ""
    assert settings.get_calibration_revision() == 0
    assert settings.get_source_calibrations() == ()


def test_historical_settings_decode_with_unknown_frequency_and_unselected_calibration() -> None:
    current = ExposureSettings(target_dose=1.0)
    historical = current.get_dict()
    historical.pop("calibration_profile_id")
    historical.pop("calibration_revision")
    historical.pop("chopper_frequency_hz")
    historical.pop("source_calibrations")

    decoded = ExposureSettings.decode(json.dumps(historical))

    assert decoded.get_chopper_frequency_hz() is None
    assert decoded.get_calibration_profile_id() == ""
    assert decoded.get_calibration_revision() == 0
    assert decoded.get_source_calibrations() == ()


def test_source_calibration_bindings_round_trip_as_json() -> None:
    binding = SourceCalibrationBinding(
        source_kind="siglent",
        source_id="scope-1",
        profile_id=uuid.uuid4(),
        revision=3,
    )
    settings = ExposureSettings(source_calibrations=(binding,))

    decoded = ExposureSettings.decode(settings.encode())

    assert decoded.get_source_calibrations() == (binding,)
    assert json.loads(settings.encode())["source_calibrations"] == [binding.to_dict()]


def test_source_calibration_bindings_reject_duplicate_source_identity() -> None:
    source = {"source_kind": "siglent", "source_id": "scope-1"}
    with pytest.raises(ValueError, match="unique source identities"):
        ExposureSettings.decode(
            json.dumps(
                ExposureSettings().get_dict()
                | {
                    "source_calibrations": [
                        source | {"profile_id": str(uuid.uuid4()), "revision": 1},
                        source | {"profile_id": str(uuid.uuid4()), "revision": 2},
                    ]
                }
            )
        )