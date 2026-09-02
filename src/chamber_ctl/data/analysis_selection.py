from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from chamber_ctl.data.calibration import SourceKey


ACTIVE_DOSE_PRODUCT_TAG = "active_dose_product"
ACTIVE_DOSE_PRODUCT_TAG_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ActiveDoseProduct:
    source_key: SourceKey
    algorithm: str
    analysis_resource: str

    def __post_init__(self) -> None:
        if not isinstance(self.source_key, SourceKey):
            raise ValueError("Active dose product must have a source key.")
        if not isinstance(self.algorithm, str) or not self.algorithm.strip():
            raise ValueError("Active dose product algorithm must be non-empty text.")
        if (
            not isinstance(self.analysis_resource, str)
            or not self.analysis_resource.strip()
            or Path(self.analysis_resource).name != self.analysis_resource
        ):
            raise ValueError("Active dose product resource must be a local filename.")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": ACTIVE_DOSE_PRODUCT_TAG_SCHEMA_VERSION,
            "source_kind": self.source_key.source_kind,
            "source_id": self.source_key.source_id,
            "algorithm": self.algorithm,
            "analysis_resource": self.analysis_resource,
        }

    @classmethod
    def from_dict(cls, value: object) -> "ActiveDoseProduct":
        expected = {
            "schema_version",
            "source_kind",
            "source_id",
            "algorithm",
            "analysis_resource",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("Active dose product contains unknown or missing fields.")
        if value["schema_version"] != ACTIVE_DOSE_PRODUCT_TAG_SCHEMA_VERSION:
            raise ValueError("Active dose product has an unsupported schema version.")
        return cls(
            SourceKey(value["source_kind"], value["source_id"]),
            value["algorithm"],
            value["analysis_resource"],
        )


def encode_active_dose_product_tag(product: ActiveDoseProduct) -> str:
    if not isinstance(product, ActiveDoseProduct):
        raise ValueError("Active dose product tag requires a product selection.")
    return json.dumps(
        product.to_dict(),
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def decode_active_dose_product_tag(value: object) -> ActiveDoseProduct:
    if not isinstance(value, str) or not value:
        raise ValueError("Active dose product tag must be non-empty JSON text.")
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("Active dose product tag is not valid JSON.") from exc
    return ActiveDoseProduct.from_dict(payload)


def active_dose_product_from_tags(tags: dict[str, object]) -> ActiveDoseProduct | None:
    value = tags.get(ACTIVE_DOSE_PRODUCT_TAG)
    return None if value is None else decode_active_dose_product_tag(value)