import json

import pytest

from chamber_ctl.data.analysis_selection import (
    ACTIVE_DOSE_PRODUCT_TAG,
    ActiveDoseProduct,
    active_dose_product_from_tags,
    decode_active_dose_product_tag,
    encode_active_dose_product_tag,
)
from chamber_ctl.data.calibration import SourceKey


def test_active_dose_product_round_trips_exact_source_and_resource() -> None:
    product = ActiveDoseProduct(
        SourceKey("siglent", "scope-1"),
        "captured",
        "euv_observer_dose_analysis_session_captured.json",
    )

    encoded = encode_active_dose_product_tag(product)

    assert decode_active_dose_product_tag(encoded) == product
    assert active_dose_product_from_tags({ACTIVE_DOSE_PRODUCT_TAG: encoded}) == product
    assert active_dose_product_from_tags({}) is None


def test_active_dose_product_rejects_unknown_fields_and_paths() -> None:
    encoded = encode_active_dose_product_tag(
        ActiveDoseProduct(SourceKey("red_pitaya", "red-pitaya"), "pitaya-v1", "dose.json")
    )
    payload = json.loads(encoded)
    payload["unexpected"] = True

    with pytest.raises(ValueError, match="unknown or missing"):
        decode_active_dose_product_tag(json.dumps(payload))
    with pytest.raises(ValueError, match="local filename"):
        ActiveDoseProduct(SourceKey("siglent", "scope-1"), "captured", "folder/dose.json")
    payload.pop("unexpected")
    payload["source_id"] = 1
    with pytest.raises(ValueError, match="source_id"):
        decode_active_dose_product_tag(json.dumps(payload))