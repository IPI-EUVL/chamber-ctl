from __future__ import annotations

import argparse
import os
import socket
import sys
import time
from collections import Counter
from pathlib import Path

from chamber_ctl.data.registry_repair import (
    DEFAULT_REPAIR_WORKERS,
    MAX_REPAIR_WORKERS,
    RepairResult,
    log_applied_repair,
    repair_exposure_registries,
)
from ipi_ecs.core import tcp
from ipi_ecs.logging.client import LogClient


ECS_LOG_ADDRESS = ("127.0.0.1", 11751)
ECS_LOG_CONNECT_TIMEOUT = 5.0
ECS_LOG_DRAIN_SECONDS = 0.1


class _SynchronousEcsLogSocket:
    def __init__(self) -> None:
        self._socket = socket.create_connection(
            ECS_LOG_ADDRESS,
            timeout=ECS_LOG_CONNECT_TIMEOUT,
        )
        self._sent = False

    def put(self, payload: bytes) -> None:
        frame = tcp.escape_bytes(payload) + tcp.DELIM
        self._socket.sendall(frame)
        self._sent = True

    def close(self) -> None:
        if self._sent:
            time.sleep(ECS_LOG_DRAIN_SECONDS)
        try:
            self._socket.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        self._socket.close()


def _default_data_path() -> Path | None:
    euvl_path = os.environ.get("EUVL_PATH")
    return Path(euvl_path) / "datasets" if euvl_path else None


def _display_id(result: RepairResult) -> str:
    return str(result.run_uuid or result.entry_uuid)


def _print_inventory(label: str, resources: tuple[tuple[str, str], ...]) -> None:
    print(f"  {label} ({len(resources)}):", flush=True)
    if not resources:
        print("    (none)", flush=True)
        return
    for filename, resource_type in resources:
        print(f"    {filename}:{resource_type}", flush=True)


def _worker_count(value: str) -> int:
    workers = int(value)
    if not 1 <= workers <= MAX_REPAIR_WORKERS:
        raise argparse.ArgumentTypeError(f"workers must be between 1 and {MAX_REPAIR_WORKERS}")
    return workers


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Reconcile exposure snapshot files with their registry declarations and rebuild missing or empty "
            "exposure registries. The default mode is a read-only dry run."
        ),
        epilog=(
            "Do not apply repairs while an experiment controller or data writer is using the dataset. "
            "Apply mode requires the ECS logger at 127.0.0.1:11751."
        ),
    )
    parser.add_argument(
        "data_path",
        nargs="?",
        type=Path,
        default=_default_data_path(),
        help="Dataset directory containing library.sqlite3 (defaults to %%EUVL_PATH%%\\datasets).",
    )
    parser.add_argument("--apply", action="store_true", help="Write repairs; without this flag no files are changed.")
    parser.add_argument(
        "--workers",
        type=_worker_count,
        default=DEFAULT_REPAIR_WORKERS,
        help=f"Parallel Box read workers (default: {DEFAULT_REPAIR_WORKERS}, maximum: {MAX_REPAIR_WORKERS}).",
    )
    args = parser.parse_args()

    if args.data_path is None:
        parser.error("data_path is required when EUVL_PATH is not set")

    audit_socket = None
    audit_logger = None
    if args.apply:
        try:
            audit_socket = _SynchronousEcsLogSocket()
        except OSError as exc:
            print(
                f"Cannot connect to the ECS logger at {ECS_LOG_ADDRESS[0]}:{ECS_LOG_ADDRESS[1]}: {exc}. "
                "No registries were scanned or modified.",
                file=sys.stderr,
            )
            return 1
        audit_logger = LogClient(audit_socket)

    counts = Counter()
    scanned_count = 0
    changed_count = 0
    print(
        f"Scanning exposure records in {args.data_path} ({'apply' if args.apply else 'dry run'}, "
        f"{args.workers} read workers)...",
        flush=True,
    )
    try:
        for result in repair_exposure_registries(
            args.data_path,
            apply=args.apply,
            max_workers=args.workers,
        ):
            scanned_count += 1
            changed_count += int(result.changed)
            counts[result.action] += 1
            disk_snapshot_detail = "unknown" if result.disk_snapshots is None else str(result.disk_snapshots)
            registered_snapshot_detail = (
                f"{result.registry_state_before} registry"
                if result.registered_snapshots is None
                else str(result.registered_snapshots)
            )
            detail = (
                f"disk snapshots={disk_snapshot_detail}, registered snapshots={registered_snapshot_detail}, "
                f"resulting resources={result.resource_count}"
            )
            print(f"[{scanned_count}] {result.action}: {_display_id(result)} ({detail})", flush=True)
            if result.message:
                print(f"  note: {result.message}", flush=True)
            _print_inventory("disk resources found", result.disk_resources)
            _print_inventory("registry resources before repair", result.registered_resources)
            _print_inventory(
                "registry declarations written" if args.apply else "registry writes planned",
                result.registry_writes,
            )
            if result.registry_only_snapshot_resources:
                _print_inventory(
                    "snapshot declarations without canonical files on disk",
                    result.registry_only_snapshot_resources,
                )
            if result.changed:
                try:
                    log_applied_repair(audit_logger, result)
                except (OSError, TypeError, ValueError) as exc:
                    print(
                        f"Registry {result.folder / 'registry.dat'} was changed, but its ECS audit log "
                        f"could not be sent: {exc}. Stopping before the next record.",
                        file=sys.stderr,
                    )
                    return 1
    finally:
        if audit_socket is not None:
            audit_socket.close()

    candidate_count = sum(
        counts[action]
        for action in (
            "would_rebuild_missing",
            "would_rebuild_empty",
            "would_reconcile_snapshots",
            "rebuilt_missing",
            "rebuilt_empty",
            "reconciled_snapshots",
        )
    )
    print(
        f"scanned={scanned_count} candidates={candidate_count} changed={changed_count} "
        f"empty_registry_candidates={counts['would_rebuild_empty'] + counts['rebuilt_empty']} "
        f"matched={counts['registry_matches_disk']} "
        f"missing_files={counts['registry_references_missing_snapshot_files']} errors={counts['error']}"
    )
    if not args.apply and candidate_count:
        print("Dry run only; re-run with --apply to write these repairs.")
    return 1 if counts["error"] else 0


if __name__ == "__main__":
    raise SystemExit(main())