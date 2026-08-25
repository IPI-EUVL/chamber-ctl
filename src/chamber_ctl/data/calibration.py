from __future__ import annotations

import hashlib
import json
import math
import threading
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

from ipi_ecs.db.db_library import Library


CALIBRATION_SCHEMA_VERSION = 1
CALIBRATION_RESOURCE = "calibration_profile.json"
CALIBRATION_RESOURCE_TYPE = "calibration_profile"
CALIBRATION_TAG = "euv_calibration_profile"


def _finite(name: str, value: float, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"{name} must be finite.")
    result = float(value)
    if positive and result <= 0:
        raise ValueError(f"{name} must be positive.")
    return result


@dataclass(frozen=True)
class CalibrationProfile:
    profile_id: uuid.UUID
    revision: int
    name: str
    created_at: float
    algorithm_version: str
    signal_polarity: int
    load_resistance_ohms: float
    photodiode_responsivity_a_per_w: float
    illuminated_area_cm2: float
    multiplicative_correction: float = 1.0
    additive_pulse_dose_mj_cm2: float = 0.0
    provenance: str = ""
    notes: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.profile_id, uuid.UUID):
            raise ValueError("profile_id must be a UUID.")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 1:
            raise ValueError("revision must be a positive integer.")
        for name in ("name", "algorithm_version", "provenance", "notes"):
            value = getattr(self, name)
            if not isinstance(value, str) or (name in ("name", "algorithm_version") and not value.strip()):
                raise ValueError(f"{name} must be non-empty text." if name in ("name", "algorithm_version") else f"{name} must be text.")
        if self.signal_polarity not in (-1, 1):
            raise ValueError("signal_polarity must be -1 or 1.")
        object.__setattr__(self, "created_at", _finite("created_at", self.created_at))
        object.__setattr__(self, "load_resistance_ohms", _finite("load_resistance_ohms", self.load_resistance_ohms, positive=True))
        object.__setattr__(
            self,
            "photodiode_responsivity_a_per_w",
            _finite("photodiode_responsivity_a_per_w", self.photodiode_responsivity_a_per_w, positive=True),
        )
        object.__setattr__(self, "illuminated_area_cm2", _finite("illuminated_area_cm2", self.illuminated_area_cm2, positive=True))
        object.__setattr__(self, "multiplicative_correction", _finite("multiplicative_correction", self.multiplicative_correction))
        object.__setattr__(
            self,
            "additive_pulse_dose_mj_cm2",
            _finite("additive_pulse_dose_mj_cm2", self.additive_pulse_dose_mj_cm2),
        )

    def _content(self) -> dict:
        return {
            "schema_version": CALIBRATION_SCHEMA_VERSION,
            "profile_id": str(self.profile_id),
            "revision": self.revision,
            "name": self.name,
            "created_at": self.created_at,
            "algorithm_version": self.algorithm_version,
            "signal_polarity": self.signal_polarity,
            "load_resistance_ohms": self.load_resistance_ohms,
            "photodiode_responsivity_a_per_w": self.photodiode_responsivity_a_per_w,
            "illuminated_area_cm2": self.illuminated_area_cm2,
            "multiplicative_correction": self.multiplicative_correction,
            "additive_pulse_dose_mj_cm2": self.additive_pulse_dose_mj_cm2,
            "provenance": self.provenance,
            "notes": self.notes,
        }

    @property
    def content_hash(self) -> str:
        payload = json.dumps(self._content(), allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def to_dict(self) -> dict:
        return self._content() | {"content_hash": self.content_hash}

    @classmethod
    def from_dict(cls, value: object) -> "CalibrationProfile":
        expected = {
            "schema_version",
            "profile_id",
            "revision",
            "name",
            "created_at",
            "algorithm_version",
            "signal_polarity",
            "load_resistance_ohms",
            "photodiode_responsivity_a_per_w",
            "illuminated_area_cm2",
            "multiplicative_correction",
            "additive_pulse_dose_mj_cm2",
            "provenance",
            "notes",
            "content_hash",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("Calibration profile contains unknown or missing fields.")
        if value["schema_version"] != CALIBRATION_SCHEMA_VERSION:
            raise ValueError("Unsupported calibration profile schema version.")
        profile = cls(
            profile_id=uuid.UUID(str(value["profile_id"])),
            revision=int(value["revision"]),
            name=str(value["name"]),
            created_at=float(value["created_at"]),
            algorithm_version=str(value["algorithm_version"]),
            signal_polarity=int(value["signal_polarity"]),
            load_resistance_ohms=float(value["load_resistance_ohms"]),
            photodiode_responsivity_a_per_w=float(value["photodiode_responsivity_a_per_w"]),
            illuminated_area_cm2=float(value["illuminated_area_cm2"]),
            multiplicative_correction=float(value["multiplicative_correction"]),
            additive_pulse_dose_mj_cm2=float(value["additive_pulse_dose_mj_cm2"]),
            provenance=str(value["provenance"]),
            notes=str(value["notes"]),
        )
        if value["content_hash"] != profile.content_hash:
            raise ValueError("Calibration profile content hash does not match its fields.")
        return profile

    def revised(self, *, created_at: float | None = None, **changes) -> "CalibrationProfile":
        return replace(self, revision=self.revision + 1, created_at=time.time() if created_at is None else created_at, **changes)

    def dose_for_integral(self, integral_volt_seconds: float) -> float:
        integral = _finite("integral_volt_seconds", integral_volt_seconds)
        base_dose = (
            ((integral * self.signal_polarity / self.load_resistance_ohms) / self.photodiode_responsivity_a_per_w)
            * 1000.0
            / self.illuminated_area_cm2
        )
        return base_dose * self.multiplicative_correction + self.additive_pulse_dose_mj_cm2


class CalibrationRepository:
    def __init__(self, data_path: str | Path) -> None:
        self._library = Library(str(data_path))
        self._owner_thread_id = threading.get_ident()

    def create(self, profile: CalibrationProfile) -> CalibrationProfile:
        self._require_owner()
        if profile.revision != 1:
            raise ValueError("New calibration profiles must begin at revision one.")
        if self.get(profile.profile_id, 1) is not None:
            raise ValueError(f"Calibration profile {profile.profile_id} revision one already exists.")
        self._write_profile(profile)
        return profile

    def save_revision(self, profile: CalibrationProfile) -> CalibrationProfile:
        self._require_owner()
        previous = self.get(profile.profile_id, profile.revision - 1)
        if previous is None:
            raise ValueError("Calibration revision must follow an existing prior revision.")
        if self.get(profile.profile_id, profile.revision) is not None:
            raise ValueError(f"Calibration profile {profile.profile_id} revision {profile.revision} already exists.")
        self._write_profile(profile)
        return profile

    def get(self, profile_id: uuid.UUID, revision: int) -> CalibrationProfile | None:
        self._require_owner()
        entries = self._library.query(
            {
                "tags": {
                    CALIBRATION_TAG: "1",
                    "calibration_profile_id": str(profile_id),
                    "calibration_revision": str(revision),
                }
            },
            limit=2,
        )
        if len(entries) > 1:
            raise ValueError(f"Multiple entries exist for calibration {profile_id} revision {revision}.")
        if not entries:
            return None
        with entries[0].resource(CALIBRATION_RESOURCE, CALIBRATION_RESOURCE_TYPE, "r") as resource:
            return CalibrationProfile.from_dict(json.load(resource))

    def list_latest(self) -> tuple[CalibrationProfile, ...]:
        self._require_owner()
        entries = self._library.query({"tags": {CALIBRATION_TAG: "1"}}, limit=None)
        latest: dict[uuid.UUID, CalibrationProfile] = {}
        for entry in entries:
            with entry.resource(CALIBRATION_RESOURCE, CALIBRATION_RESOURCE_TYPE, "r") as resource:
                profile = CalibrationProfile.from_dict(json.load(resource))
            current = latest.get(profile.profile_id)
            if current is None or profile.revision > current.revision:
                latest[profile.profile_id] = profile
        return tuple(sorted(latest.values(), key=lambda profile: (profile.name.casefold(), str(profile.profile_id))))

    def close(self) -> None:
        self._require_owner()
        self._library.close()

    def _write_profile(self, profile: CalibrationProfile) -> None:
        entry = self._library.create_entry(
            f"Calibration: {profile.name} r{profile.revision}",
            profile.provenance or "EUV photodiode calibration profile",
        )
        entry.set_tag(CALIBRATION_TAG, "1")
        entry.set_tag("calibration_profile_id", str(profile.profile_id))
        entry.set_tag("calibration_revision", str(profile.revision))
        entry.set_tag("calibration_name", profile.name)
        entry.set_tag("calibration_hash", profile.content_hash)
        with entry.resource(CALIBRATION_RESOURCE, CALIBRATION_RESOURCE_TYPE, "w") as resource:
            json.dump(profile.to_dict(), resource, allow_nan=False, separators=(",", ":"))

    def _require_owner(self) -> None:
        if threading.get_ident() != self._owner_thread_id:
            raise RuntimeError("Calibration repository used from a non-owning thread.")