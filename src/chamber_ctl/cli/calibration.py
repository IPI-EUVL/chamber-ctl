from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from pathlib import Path

from chamber_ctl.data.calibration import CalibrationProfile, CalibrationRepository


def _data_path(value: str | None) -> Path:
    if value:
        return Path(value)
    root = os.environ.get("EUVL_PATH")
    if not root:
        raise ValueError("--data-path is required when EUVL_PATH is not set.")
    return Path(root) / "datasets"


def _add_profile_arguments(parser: argparse.ArgumentParser, *, revise: bool) -> None:
    parser.add_argument("--name", required=not revise)
    parser.add_argument("--algorithm-version", required=not revise)
    parser.add_argument("--signal-polarity", type=int, choices=(-1, 1))
    parser.add_argument("--load-resistance-ohms", type=float)
    parser.add_argument("--responsivity-a-per-w", type=float)
    parser.add_argument("--illuminated-area-cm2", type=float)
    parser.add_argument("--multiplicative-correction", type=float)
    parser.add_argument("--additive-pulse-dose-mj-cm2", type=float)
    parser.add_argument("--provenance")
    parser.add_argument("--notes")


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("calibration", help="Manage immutable EUV photodiode calibration profiles.")
    parser.add_argument("--data-path", help="Experiment database directory; defaults to %%EUVL_PATH%%\\datasets.")
    commands = parser.add_subparsers(dest="calibration_command", required=True)

    create = commands.add_parser("create", help="Create calibration revision one.")
    _add_profile_arguments(create, revise=False)
    create.set_defaults(func=main)

    revise = commands.add_parser("revise", help="Append a revision to an existing profile.")
    revise.add_argument("profile_id")
    revise.add_argument("revision", type=int)
    _add_profile_arguments(revise, revise=True)
    revise.set_defaults(func=main)

    list_parser = commands.add_parser("list", help="List latest calibration profile revisions.")
    list_parser.set_defaults(func=main)

    show = commands.add_parser("show", help="Display an exact calibration revision.")
    show.add_argument("profile_id")
    show.add_argument("revision", type=int)
    show.set_defaults(func=main)


def _profile_from_create_args(args: argparse.Namespace) -> CalibrationProfile:
    return CalibrationProfile(
        profile_id=uuid.uuid4(),
        revision=1,
        name=args.name,
        created_at=time.time(),
        algorithm_version=args.algorithm_version,
        signal_polarity=args.signal_polarity,
        load_resistance_ohms=args.load_resistance_ohms,
        photodiode_responsivity_a_per_w=args.responsivity_a_per_w,
        illuminated_area_cm2=args.illuminated_area_cm2,
        multiplicative_correction=1.0 if args.multiplicative_correction is None else args.multiplicative_correction,
        additive_pulse_dose_mj_cm2=0.0 if args.additive_pulse_dose_mj_cm2 is None else args.additive_pulse_dose_mj_cm2,
        provenance=args.provenance or "",
        notes=args.notes or "",
    )


def _profile_changes(args: argparse.Namespace, profile: CalibrationProfile) -> dict:
    fields = {
        "name": args.name,
        "algorithm_version": args.algorithm_version,
        "signal_polarity": args.signal_polarity,
        "load_resistance_ohms": args.load_resistance_ohms,
        "photodiode_responsivity_a_per_w": args.responsivity_a_per_w,
        "illuminated_area_cm2": args.illuminated_area_cm2,
        "multiplicative_correction": args.multiplicative_correction,
        "additive_pulse_dose_mj_cm2": args.additive_pulse_dose_mj_cm2,
        "provenance": args.provenance,
        "notes": args.notes,
    }
    return {key: value for key, value in fields.items() if value is not None and value != getattr(profile, key)}


def main(args: argparse.Namespace) -> int:
    repository = CalibrationRepository(_data_path(args.data_path))
    try:
        if args.calibration_command == "create":
            profile = repository.create(_profile_from_create_args(args))
            print(json.dumps(profile.to_dict(), indent=2, sort_keys=True))
            return 0
        if args.calibration_command == "revise":
            profile_id = uuid.UUID(args.profile_id)
            current = repository.get(profile_id, args.revision)
            if current is None:
                raise ValueError(f"Calibration profile {profile_id} revision {args.revision} was not found.")
            changes = _profile_changes(args, current)
            if not changes:
                raise ValueError("A revision must change at least one calibration field.")
            revised = repository.save_revision(current.revised(**changes))
            print(json.dumps(revised.to_dict(), indent=2, sort_keys=True))
            return 0
        if args.calibration_command == "list":
            print(json.dumps([profile.to_dict() for profile in repository.list_latest()], indent=2, sort_keys=True))
            return 0
        if args.calibration_command == "show":
            profile = repository.get(uuid.UUID(args.profile_id), args.revision)
            if profile is None:
                raise ValueError(f"Calibration profile {args.profile_id} revision {args.revision} was not found.")
            print(json.dumps(profile.to_dict(), indent=2, sort_keys=True))
            return 0
        raise ValueError(f"Unknown calibration command {args.calibration_command!r}.")
    finally:
        repository.close()