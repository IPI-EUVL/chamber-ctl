from dataclasses import replace
import threading
import uuid

import pytest

from chamber_ctl.data.calibration import CalibrationProfile, CalibrationRepository


def _profile(**changes) -> CalibrationProfile:
    values = {
        "profile_id": uuid.uuid4(),
        "revision": 1,
        "name": "EUV PD",
        "created_at": 100.0,
        "algorithm_version": "dose-v1-native-integral",
        "signal_polarity": 1,
        "load_resistance_ohms": 50.0,
        "photodiode_responsivity_a_per_w": 0.14,
        "illuminated_area_cm2": 0.05,
        "multiplicative_correction": 1.5,
        "additive_pulse_dose_mj_cm2": 0.02,
        "provenance": "calibration fixture",
        "notes": "test",
    }
    values.update(changes)
    return CalibrationProfile(**values)


def test_calibration_hash_is_stable_and_rejects_tampering() -> None:
    profile = _profile()
    encoded = profile.to_dict()

    assert CalibrationProfile.from_dict(encoded) == profile
    encoded["multiplicative_correction"] = 2.0
    with pytest.raises(ValueError, match="hash"):
        CalibrationProfile.from_dict(encoded)


def test_calibration_converts_native_integral_in_the_agreed_order() -> None:
    profile = _profile()

    dose = profile.dose_for_integral(0.14)

    expected = (((0.14 / 50.0) / 0.14) * 1000.0 / 0.05) * 1.5 + 0.02
    assert dose == pytest.approx(expected)


def test_calibration_repository_persists_immutable_revisions(tmp_path) -> None:
    repository = CalibrationRepository(tmp_path)
    first = _profile()
    second = first.revised(created_at=110.0, notes="revised")
    try:
        repository.create(first)
        repository.save_revision(second)

        assert repository.get(first.profile_id, 1) == first
        assert repository.get(first.profile_id, 2) == second
        assert repository.list_all() == (first, second)
        assert repository.list_latest() == (second,)
        with pytest.raises(ValueError, match="already exists"):
            repository.save_revision(second)
    finally:
        repository.close()


def test_calibration_repository_enforces_thread_affinity(tmp_path) -> None:
    repository = CalibrationRepository(tmp_path)
    errors = []
    try:
        thread = threading.Thread(target=lambda: errors.append(_other_thread_get(repository)))
        thread.start()
        thread.join()
        assert isinstance(errors[0], RuntimeError)
    finally:
        repository.close()


def _other_thread_get(repository):
    try:
        repository.list_latest()
    except Exception as exc:
        return exc
    raise AssertionError("Thread-affinity error was expected")