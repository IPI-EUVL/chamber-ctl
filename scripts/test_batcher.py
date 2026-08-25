from __future__ import annotations

__test__ = False

import json
import math
import os
import sys
import uuid
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from chamber_ctl import ECS_IP
from chamber_ctl.subsystems.batcher import (
    BatchCoordinator,
    BatchHistoryStore,
    BatchPlan,
    BatchPlanEntry,
    DdsControllerStateSource,
    ExposureTemplate,
    TargetMode,
)
from chamber_ctl.subsystems.settings_presets import SettingsPresets


def _prompt_text(label: str, *, required: bool = False, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        value = input(f"{label}{suffix}: ").strip()
        if value:
            return value
        if default:
            return default
        if not required:
            return ""
        print(f"{label} is required.")


def _prompt_float(label: str, *, default: float | None = None, positive: bool = False) -> float:
    suffix = f" [{default:g}]" if default is not None else ""
    while True:
        value = input(f"{label}{suffix}: ").strip()
        if not value and default is not None:
            return default
        try:
            parsed = float(value)
        except ValueError:
            print("Enter a numeric value.")
            continue
        if not math.isfinite(parsed):
            print("Value must be finite.")
            continue
        if positive and parsed <= 0:
            print("Value must be greater than zero.")
            continue
        return parsed


def _prompt_preset(label: str, values: list[str]) -> str:
    options = [value for value in values if str(value).strip()]
    if options:
        print(f"{label} presets:")
        for index, value in enumerate(options, start=1):
            print(f"  {index}. {value}")
        print("Enter a preset number or a custom value.")
    return_value = _prompt_text(label)
    if return_value.isdigit():
        index = int(return_value) - 1
        if 0 <= index < len(options):
            return options[index]
    return return_value


def _collect_entries() -> tuple[BatchPlanEntry, ...]:
    entries = []
    used_slots = set()
    while True:
        raw_slot = input("Sample slot 1-12 (or 'done'): ").strip().lower()
        if raw_slot in ("done", "d", ""):
            if entries:
                return tuple(entries)
            print("Add at least one sample before finishing.")
            continue
        try:
            slot = int(raw_slot)
        except ValueError:
            print("Enter an integer slot from 1 through 12.")
            continue
        if not 1 <= slot <= 12:
            print("Sample slot must be from 1 through 12.")
            continue
        if slot in used_slots:
            print(f"Sample slot {slot} is already in this plan.")
            continue

        while True:
            raw_mode = input("Target mode, dose or time [dose]: ").strip().lower() or "dose"
            if raw_mode in ("dose", "d"):
                mode = TargetMode.DOSE
                unit = "mJ/cm2"
                break
            if raw_mode in ("time", "t"):
                mode = TargetMode.TIME
                unit = "seconds"
                break
            print("Choose 'dose' or 'time'.")

        target = _prompt_float(f"Target {mode.value} ({unit})", positive=True)
        entries.append(BatchPlanEntry(sample=slot - 1, mode=mode, target=target))
        used_slots.add(slot)


def collect_plan(data_path: str) -> BatchPlan:
    presets = SettingsPresets(data_path)
    try:
        operators = presets.read_operators()
        zr_filters = presets.read_zr_filters()
        sample_types = presets.read_sample_types()
    finally:
        presets.close()

    print("\nShared exposure settings")
    template = ExposureTemplate(
        name=_prompt_text("Name", required=True),
        description=_prompt_text("Description"),
        operator=_prompt_preset("Operator", operators),
        zr_filter=_prompt_preset("Zr filter", zr_filters),
        sample_type=_prompt_preset("Sample type", sample_types),
        base_pressure=_prompt_float("Base pressure", default=0.0),
        operating_pressure=_prompt_float("Operating pressure", default=0.0),
        flow_sccm=_prompt_float("Flow (sccm)", default=0.0),
    )
    print("\nSample targets")
    return BatchPlan(template=template, entries=_collect_entries())


def _plan_payload(plan: BatchPlan, batch_uuid: uuid.UUID) -> dict:
    return {
        "batch_uuid": str(batch_uuid),
        "shared": {
            "name": plan.template.name,
            "description": plan.template.description,
            "operator": plan.template.operator,
            "zr_filter": plan.template.zr_filter,
            "sample_type": plan.template.sample_type,
            "base_pressure": plan.template.base_pressure,
            "operating_pressure": plan.template.operating_pressure,
            "flow_sccm": plan.template.flow_sccm,
        },
        "samples": [
            {
                "slot": entry.sample + 1,
                "stored_sample": entry.sample,
                "mode": entry.mode.value,
                "target": entry.target,
            }
            for entry in plan.entries
        ],
    }


def _run_commands(coordinator: BatchCoordinator) -> None:
    print("\nCommands: status, start, pause, resume, quit")
    while True:
        try:
            command = input("batch> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if command in ("quit", "exit", "q"):
            return
        if command in ("status", "s"):
            print(coordinator.status())
            continue
        if command in ("start", "run"):
            coordinator.start_next()
            continue
        if command in ("pause", "p"):
            coordinator.pause()
            print("Manual pause requested.")
            coordinator.poll_once()
            continue
        if command in ("resume", "r"):
            coordinator.resume()
            print("Resume requested; the currently displayed failed run is acknowledged in memory.")
            coordinator.poll_once()
            continue
        if command:
            print("Unknown command. Use status, start, pause, resume, or quit.")


def main() -> int:
    euvl_path = os.environ.get("EUVL_PATH")
    if not euvl_path:
        print("EUVL_PATH is not set.", file=sys.stderr)
        return 2
    data_path = str(Path(euvl_path) / "datasets")

    plan = collect_plan(data_path)
    batch_uuid = uuid.uuid4()
    print("\nGenerated in-memory test plan:")
    print(json.dumps(_plan_payload(plan, batch_uuid), indent=2))
    print(
        "\nTo simulate attempts, add this exact tag to each matching exposure record:\n"
        f"  batch_uuid = {batch_uuid}\n"
        "Stored target_dose and target_time tags are ignored by this planner.\n"
        "WARNING: this test script recalculates and writes dose/runtime tags when required; "
        "only the explicit 'start' command writes controller settings and starts an exposure."
    )

    history = None
    controller = None
    coordinator = None
    try:
        history = BatchHistoryStore(data_path)
        controller = DdsControllerStateSource(ECS_IP)
        coordinator = BatchCoordinator(plan, batch_uuid, history, controller)
        coordinator.start()
        _run_commands(coordinator)
    finally:
        if coordinator is not None:
            coordinator.close()
        else:
            if controller is not None:
                controller.close()
            if history is not None:
                history.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())