import argparse
import json

from chamber_ctl.cli.calibration import main


def _args(data_path, command, **values):
    return argparse.Namespace(data_path=str(data_path), calibration_command=command, **values)


def test_calibration_cli_creates_lists_and_revises_profiles(tmp_path, capsys) -> None:
    create = _args(
        tmp_path,
        "create",
        name="CLI profile",
        algorithm_version="dose-v1",
        signal_polarity=1,
        load_resistance_ohms=50.0,
        responsivity_a_per_w=0.14,
        illuminated_area_cm2=0.05,
        multiplicative_correction=None,
        additive_pulse_dose_mj_cm2=None,
        provenance="fixture",
        notes="",
    )
    assert main(create) == 0
    created = json.loads(capsys.readouterr().out)

    assert main(_args(tmp_path, "list")) == 0
    assert json.loads(capsys.readouterr().out)[0]["profile_id"] == created["profile_id"]

    revise = _args(
        tmp_path,
        "revise",
        profile_id=created["profile_id"],
        revision=1,
        name=None,
        algorithm_version=None,
        signal_polarity=None,
        load_resistance_ohms=None,
        responsivity_a_per_w=None,
        illuminated_area_cm2=None,
        multiplicative_correction=2.0,
        additive_pulse_dose_mj_cm2=None,
        provenance=None,
        notes=None,
    )
    assert main(revise) == 0
    revised = json.loads(capsys.readouterr().out)
    assert revised["revision"] == 2
    assert revised["multiplicative_correction"] == 2.0