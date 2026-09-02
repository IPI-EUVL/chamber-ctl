import json
import uuid

import pytest

from chamber_ctl.data.calibration import (
    PRIMARY_SOURCE_TAG,
    SOURCE_CALIBRATIONS_TAG,
    SourceCalibrationBinding,
    SourceKey,
    decode_primary_source_tag,
    decode_source_calibrations_tag,
    source_calibration_for_source,
    source_calibration_run_tags,
    source_configuration_from_run_tags,
    source_configuration_run_tags,
)


def test_source_calibration_tag_is_scalar_and_matches_by_source_identity() -> None:
    first = SourceCalibrationBinding("integrator", "pulse-1", uuid.uuid4(), 2)
    second = SourceCalibrationBinding("siglent", "scope-1", uuid.uuid4(), 4)

    tags = source_calibration_run_tags((second, first))

    assert set(tags) == {SOURCE_CALIBRATIONS_TAG}
    assert isinstance(tags[SOURCE_CALIBRATIONS_TAG], str)
    assert json.loads(tags[SOURCE_CALIBRATIONS_TAG])["schema_version"] == 1
    assert source_calibration_for_source(tags[SOURCE_CALIBRATIONS_TAG], first.source_key) == first
    assert source_calibration_for_source(tags[SOURCE_CALIBRATIONS_TAG], second.source_key) == second
    assert source_calibration_for_source(
        tags[SOURCE_CALIBRATIONS_TAG],
        SourceKey("siglent", "another-scope"),
    ) is None


def test_source_calibration_tag_rejects_duplicate_source_identity() -> None:
    profile_id = uuid.uuid4()
    payload = json.dumps(
        {
            "schema_version": 1,
            "bindings": [
                {
                    "source_kind": "siglent",
                    "source_id": "scope-1",
                    "profile_id": str(profile_id),
                    "revision": 1,
                },
                {
                    "source_kind": "siglent",
                    "source_id": "scope-1",
                    "profile_id": str(profile_id),
                    "revision": 2,
                },
            ],
        }
    )

    with pytest.raises(ValueError, match="unique source identities"):
        decode_source_calibrations_tag(payload)


def test_source_configuration_selects_primary_by_exact_key_not_binding_order() -> None:
    primary = SourceCalibrationBinding("red_pitaya", "red-pitaya", uuid.uuid4(), 2)
    observer = SourceCalibrationBinding("siglent", "scope-1", uuid.uuid4(), 4)

    tags = source_configuration_run_tags((observer, primary), primary.source_key)

    assert set(tags) == {SOURCE_CALIBRATIONS_TAG, PRIMARY_SOURCE_TAG}
    assert decode_primary_source_tag(tags[PRIMARY_SOURCE_TAG]) == primary.source_key
    assert source_calibration_for_source(
        tags[SOURCE_CALIBRATIONS_TAG],
        decode_primary_source_tag(tags[PRIMARY_SOURCE_TAG]),
    ) == primary
    assert source_configuration_from_run_tags(tags).calibration_for(primary.source_key) == primary


def test_source_configuration_requires_primary_calibration_binding() -> None:
    observer = SourceCalibrationBinding("siglent", "scope-1", uuid.uuid4(), 4)

    with pytest.raises(ValueError, match="Primary source must have"):
        source_configuration_run_tags((observer,), SourceKey("red_pitaya", "red-pitaya"))


def test_source_configuration_rejects_partial_run_tags() -> None:
    with pytest.raises(ValueError, match="must be provided together"):
        source_configuration_from_run_tags(
            {PRIMARY_SOURCE_TAG: '{"schema_version":1,"source_id":"one","source_kind":"scope"}'}
        )