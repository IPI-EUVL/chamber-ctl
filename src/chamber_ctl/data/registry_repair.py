from __future__ import annotations

import queue
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from ipi_ecs.db.db_library import Library
from ipi_ecs.db.registry import (
    RegistryContents,
    is_registry_artifact,
    parse_registry,
    read_registry as read_ecs_registry,
    replace_registry,
    serialize_registry,
)


EXPERIMENT_TYPE = "exposure"
REGISTRY_FILENAME = "registry.dat"
RECOVERED_RESOURCE_TYPE = "recovered"
DEFAULT_REPAIR_WORKERS = 8
MAX_REPAIR_WORKERS = 20
REPAIR_PREFETCH_FACTOR = 2

KNOWN_RESOURCE_TYPES = {
    "run.json": "run_state",
    "metadata.json": "metadata",
    "end_metadata.json": "metadata",
    "motion_state.bin": "Motion State",
    "laser_config.bin": "Laser Config",
    "queue_state.bin": "Queue State",
    "ellipsometry.json": "Elllipsometry data",
    "euv_exposure_dose_graph.h5": "euv_exposure_dose_graph",
    "euv_capture_cadence.h5": "euv_capture_cadence",
}


@dataclass(frozen=True)
class RepairResult:
    entry_uuid: uuid.UUID
    run_uuid: uuid.UUID | None
    folder: Path
    action: str
    changed: bool
    registered_snapshots: int | None
    disk_snapshots: int | None
    resource_count: int
    registry_state_before: str = "valid"
    registry_size_before: int | None = None
    disk_resources: tuple[tuple[str, str], ...] = ()
    registered_resources: tuple[tuple[str, str], ...] = ()
    registry_after: tuple[tuple[str, str], ...] = ()
    registry_writes: tuple[tuple[str, str], ...] = ()
    registry_only_snapshot_resources: tuple[tuple[str, str], ...] = ()
    message: str = ""


@dataclass(frozen=True)
class _InspectionJob:
    entry_uuid: uuid.UUID
    run_uuid: uuid.UUID | None
    foldername: str


@dataclass(frozen=True)
class _RepairPlan:
    result: RepairResult
    registry: RegistryContents | None = None
    expected_contents: bytes | None = None


@dataclass(frozen=True)
class _InspectionOutcome:
    job: _InspectionJob
    plan: _RepairPlan | None = None
    error: Exception | None = None


class LogSink(Protocol):
    def log(self, msg: str, **kwargs: Any) -> None: ...


def _snapshot_resource_type(filename: str) -> str | None:
    if not filename.startswith("snap_"):
        return None

    if filename.endswith(".npz"):
        snapshot_id = filename[5:-4]
        resource_type = "snapshot"
    elif filename.endswith(".h5"):
        snapshot_id = filename[5:-3]
        resource_type = "euv_snapshot"
    elif filename.endswith(".json"):
        snapshot_id = filename[5:-5]
        resource_type = "snap_meta"
    else:
        return None

    try:
        uuid.UUID(snapshot_id)
    except ValueError:
        return None
    return resource_type


def infer_resource_type(filename: str) -> str:
    snapshot_type = _snapshot_resource_type(filename)
    if snapshot_type is not None:
        return snapshot_type
    return KNOWN_RESOURCE_TYPES.get(filename, RECOVERED_RESOURCE_TYPE)


def _parse_registry(contents: bytes, path: Path) -> RegistryContents:
    return parse_registry(contents, path)


def read_registry(path: Path) -> RegistryContents:
    return read_ecs_registry(path)


def _serialize_registry(registry: RegistryContents) -> str:
    return serialize_registry(registry).decode("utf-8")


def _is_registry_artifact(filename: str) -> bool:
    return is_registry_artifact(filename)


def _discover_resources(folder: Path) -> dict[str, str]:
    resources = {}
    for path in sorted(folder.iterdir(), key=lambda item: item.name.casefold()):
        if path.is_file() and not _is_registry_artifact(path.name):
            resources[path.name] = infer_resource_type(path.name)
    return resources


def _snapshot_count(resources: dict[str, str]) -> int:
    return sum(
        1
        for filename, resource_type in resources.items()
        if resource_type in {"snapshot", "euv_snapshot"} and _snapshot_resource_type(filename) in {"snapshot", "euv_snapshot"}
    )


def _inventory(resources: dict[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(resources.items(), key=lambda item: item[0].casefold()))


def _disk_snapshot_resources(resources: dict[str, str]) -> dict[str, str]:
    return {
        filename: resource_type
        for filename, resource_type in resources.items()
        if _snapshot_resource_type(filename) is not None
    }


def _registered_snapshot_resources(resources: dict[str, str]) -> dict[str, str]:
    return {
        filename: resource_type
        for filename, resource_type in resources.items()
        if _snapshot_resource_type(filename) is not None or resource_type in {"snapshot", "snap_meta", "euv_snapshot"}
    }


def _next_backup_path(registry_path: Path) -> Path:
    base = registry_path.with_name(f"{registry_path.name}.pre-repair")
    candidate = base
    suffix = 1
    while candidate.exists():
        candidate = base.with_name(f"{base.name}.{suffix}")
        suffix += 1
    return candidate


def _atomic_write_registry(
    registry_path: Path,
    registry: RegistryContents,
    expected_contents: bytes | None,
) -> None:
    replace_registry(
        registry_path,
        registry,
        expected_contents=expected_contents,
        backup_path_factory=_next_backup_path if expected_contents is not None else None,
    )


def _entry_folder(data_path: Path, foldername: str) -> Path:
    root = data_path.resolve()
    folder = (root / foldername).resolve()
    if folder != root and root not in folder.parents:
        raise ValueError(f"Entry folder is outside the data path: {foldername}")
    if not folder.is_dir():
        raise FileNotFoundError(f"Entry folder does not exist: {folder}")
    return folder


def _run_uuid(entry) -> uuid.UUID | None:
    value = entry.get_tags().get("run")
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _inspect_entry(data_path: Path, entry) -> _RepairPlan:
    folder = _entry_folder(data_path, entry.get_foldername())
    registry_path = folder / REGISTRY_FILENAME
    run_uuid = _run_uuid(entry)
    disk_resources = _discover_resources(folder)
    disk_snapshot_resources = _disk_snapshot_resources(disk_resources)
    disk_snapshots = _snapshot_count(disk_resources)

    original_contents = registry_path.read_bytes() if registry_path.exists() else None
    if original_contents is None or not original_contents:
        registry_state_before = "missing" if original_contents is None else "empty"
        action = "would_rebuild_missing" if original_contents is None else "would_rebuild_empty"
        registry = RegistryContents(
            entry_uuid=entry.get_uuid(),
            name=entry.get_name(),
            created=int(entry.get_timestamp()),
            description=entry.get_description() or "",
            resources=disk_resources,
        )
        return _RepairPlan(
            result=RepairResult(
                entry_uuid=entry.get_uuid(),
                run_uuid=run_uuid,
                folder=folder,
                action=action,
                changed=False,
                registered_snapshots=None,
                disk_snapshots=disk_snapshots,
                resource_count=len(registry.resources),
                registry_state_before=registry_state_before,
                registry_size_before=None if original_contents is None else 0,
                disk_resources=_inventory(disk_resources),
                registry_after=_inventory(registry.resources),
                registry_writes=_inventory(disk_resources),
            ),
            registry=registry,
            expected_contents=original_contents,
        )

    registry = _parse_registry(original_contents, registry_path)
    if registry.entry_uuid != entry.get_uuid():
        raise ValueError(
            f"Registry UUID {registry.entry_uuid} does not match indexed entry UUID {entry.get_uuid()}"
        )

    registered_resources = registry.resources.copy()
    registered_snapshot_resources = _registered_snapshot_resources(registered_resources)
    registered_snapshots = _snapshot_count(registered_resources)
    registry_writes = {
        filename: resource_type
        for filename, resource_type in disk_snapshot_resources.items()
        if registered_resources.get(filename) != resource_type
    }
    registry_only_snapshot_resources = {
        filename: resource_type
        for filename, resource_type in registered_snapshot_resources.items()
        if filename not in disk_snapshot_resources
    }

    registry.resources.update(registry_writes)
    if registry_writes:
        action = "would_reconcile_snapshots"
    elif registry_only_snapshot_resources:
        action = "registry_references_missing_snapshot_files"
    else:
        action = "registry_matches_disk"

    message = ""
    if registry_only_snapshot_resources:
        message = (
            f"Registry has {len(registry_only_snapshot_resources)} snapshot resource declaration(s) "
            "without a canonical snapshot file on disk; these declarations were preserved."
        )
    return _RepairPlan(
        result=RepairResult(
            entry_uuid=entry.get_uuid(),
            run_uuid=run_uuid,
            folder=folder,
            action=action,
            changed=False,
            registered_snapshots=registered_snapshots,
            disk_snapshots=disk_snapshots,
            resource_count=len(registry.resources),
            registry_size_before=len(original_contents),
            disk_resources=_inventory(disk_resources),
            registered_resources=_inventory(registered_resources),
            registry_after=_inventory(registry.resources),
            registry_writes=_inventory(registry_writes),
            registry_only_snapshot_resources=_inventory(registry_only_snapshot_resources),
            message=message,
        ),
        registry=registry if registry_writes else None,
        expected_contents=original_contents if registry_writes else None,
    )


def _error_result(data_path: Path, job: _InspectionJob, exc: Exception) -> RepairResult:
    return RepairResult(
        entry_uuid=job.entry_uuid,
        run_uuid=job.run_uuid,
        folder=data_path / job.foldername,
        action="error",
        changed=False,
        registered_snapshots=None,
        disk_snapshots=None,
        resource_count=0,
        message=f"{type(exc).__name__}: {exc}",
    )


def _apply_plan(plan: _RepairPlan, *, apply: bool) -> RepairResult:
    result = plan.result
    if not apply or plan.registry is None:
        return result

    try:
        _atomic_write_registry(
            result.folder / REGISTRY_FILENAME,
            plan.registry,
            plan.expected_contents,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        return replace(
            result,
            action="error",
            changed=False,
            message=f"{type(exc).__name__}: {exc}",
        )

    applied_action = {
        "would_rebuild_missing": "rebuilt_missing",
        "would_rebuild_empty": "rebuilt_empty",
        "would_reconcile_snapshots": "reconciled_snapshots",
    }[result.action]
    return replace(result, action=applied_action, changed=True)


_WORKER_STOP = object()


def _inspection_worker(
    data_path: Path,
    jobs: queue.Queue,
    outcomes: queue.Queue,
    ready: queue.Queue,
) -> None:
    library = None
    try:
        library = Library(str(data_path), read_only=True)
    except Exception as exc:
        ready.put((False, exc))
        return

    ready.put((True, None))
    try:
        while True:
            job = jobs.get()
            if job is _WORKER_STOP:
                return

            try:
                entry = library.read_entry(job.entry_uuid)
                plan = _inspect_entry(data_path, entry)
            except Exception as exc:
                outcomes.put(_InspectionOutcome(job=job, error=exc))
            else:
                outcomes.put(_InspectionOutcome(job=job, plan=plan))
    finally:
        library.close()


def _stop_inspection_workers(jobs: queue.Queue, threads: list[threading.Thread]) -> None:
    while True:
        try:
            jobs.get_nowait()
        except queue.Empty:
            break
    for _thread in threads:
        jobs.put(_WORKER_STOP)
    for thread in threads:
        thread.join()


def log_applied_repair(logger: LogSink, result: RepairResult) -> bool:
    if not result.changed:
        return False

    registry_before = dict(result.registered_resources) if result.registry_state_before == "valid" else None
    registry_after = dict(result.registry_after)
    disk_resources = dict(result.disk_resources)
    before_snapshot_count = (
        None if registry_before is None else _snapshot_count(registry_before)
    )
    after_snapshot_count = _snapshot_count(registry_after)
    record_id = result.run_uuid or result.entry_uuid
    action_text = {
        "rebuilt_missing": "rebuilt a missing registry",
        "rebuilt_empty": "rebuilt an empty registry",
        "reconciled_snapshots": "reconciled snapshot declarations",
    }.get(result.action, result.action.replace("_", " "))
    if result.registry_state_before == "missing":
        before_text = "missing"
    elif result.registry_state_before == "empty":
        before_text = "empty (0 bytes)"
    else:
        before_text = f"{len(registry_before)} resources and {before_snapshot_count} snapshot waveforms"
    message = (
        f"Repaired exposure registry for run {record_id}: {action_text}; "
        f"registry changed from {before_text} to {len(registry_after)} resources and "
        f"{after_snapshot_count} snapshot waveforms."
    )

    logger.log(
        message,
        level="INFO",
        l_type="DATOP",
        event="exposure_registry_repair",
        subsystem="Exposure Registry Repair",
        experiment_type=EXPERIMENT_TYPE,
        action=result.action,
        entry_uuid=str(result.entry_uuid),
        run_uuid=str(result.run_uuid) if result.run_uuid is not None else None,
        registry_path=str(result.folder / REGISTRY_FILENAME),
        before={
            "exists": result.registry_state_before != "missing",
            "state": result.registry_state_before,
            "size_bytes": result.registry_size_before,
            "resource_count": None if registry_before is None else len(registry_before),
            "snapshot_waveform_count": before_snapshot_count,
            "resources": registry_before,
        },
        after={
            "exists": True,
            "resource_count": len(registry_after),
            "snapshot_waveform_count": after_snapshot_count,
            "resources": registry_after,
        },
        disk={
            "resource_count": len(disk_resources),
            "snapshot_waveform_count": _snapshot_count(disk_resources),
            "resources": disk_resources,
        },
        registry_writes=dict(result.registry_writes),
    )
    return True


def repair_exposure_registries(
    data_path: str | Path,
    *,
    apply: bool = False,
    max_workers: int = DEFAULT_REPAIR_WORKERS,
) -> Iterator[RepairResult]:
    if isinstance(max_workers, bool) or not isinstance(max_workers, int) or not 1 <= max_workers <= MAX_REPAIR_WORKERS:
        raise ValueError(f"Registry repair workers must be between 1 and {MAX_REPAIR_WORKERS}.")

    root = Path(data_path)
    library = Library(str(root), read_only=True)
    jobs = None
    threads = []
    try:
        filters = {"tags": {"experiment": EXPERIMENT_TYPE, "run": None}}
        max_pending = max_workers * REPAIR_PREFETCH_FACTOR
        entries = library.query(filters, limit=max_pending)
        if not entries:
            return

        worker_count = min(max_workers, len(entries))
        jobs = queue.Queue(maxsize=max_pending)
        outcomes = queue.Queue()
        ready = queue.Queue()
        threads = [
            threading.Thread(
                target=_inspection_worker,
                args=(root, jobs, outcomes, ready),
                name=f"exposure-registry-read-{worker_index + 1}",
                daemon=True,
            )
            for worker_index in range(worker_count)
        ]
        for thread in threads:
            thread.start()

        startup_errors = []
        for _thread in threads:
            ok, value = ready.get()
            if not ok:
                startup_errors.append(value)
        if startup_errors:
            raise RuntimeError(f"Could not initialize registry repair worker: {startup_errors[0]}")

        pending = 0
        cursor = None
        exhausted = False
        while not exhausted or pending:
            while not exhausted and pending < max_pending:
                if entries:
                    entry = entries.pop(0)
                else:
                    entries = library.query(filters, limit=1, cursor=cursor)
                    if not entries:
                        exhausted = True
                        break
                    entry = entries.pop(0)

                cursor = (entry.get_timestamp(), entry.get_uuid())
                jobs.put(
                    _InspectionJob(
                        entry_uuid=entry.get_uuid(),
                        run_uuid=_run_uuid(entry),
                        foldername=entry.get_foldername(),
                    )
                )
                pending += 1

            if pending:
                outcome = outcomes.get()
                pending -= 1
                if outcome.error is not None:
                    yield _error_result(root, outcome.job, outcome.error)
                else:
                    yield _apply_plan(outcome.plan, apply=apply)
    finally:
        if jobs is not None:
            _stop_inspection_workers(jobs, threads)
        library.close()