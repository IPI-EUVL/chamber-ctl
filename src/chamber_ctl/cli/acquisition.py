from __future__ import annotations

import argparse
import json
import threading
import time
import uuid

from ipi_ecs.dds import client

from chamber_ctl import ECS_IP
from chamber_ctl.subsystems import uuids


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("acquisition", help="Operate EUV acquisition safety controls.")
    commands = parser.add_subparsers(dest="acquisition_command", required=True)

    resume = commands.add_parser("resume-interlock", help="Authorize motion recovery after stable pulse recovery.")
    resume.add_argument("--timeout-seconds", type=float, default=15.0)
    resume.set_defaults(func=main)

    recover = commands.add_parser("recover-orphan", help="Import, reconcile, and release an orphaned capture session.")
    recover.add_argument("--confirm", action="store_true", help="Required acknowledgement before artifacts can be imported and released.")
    recover.add_argument("--timeout-seconds", type=float, default=120.0)
    recover.set_defaults(func=main)

    test_start = commands.add_parser("test-start", help="Start continuous diagnostic acquisition.")
    test_start.add_argument("--timeout-seconds", type=float, default=15.0)
    test_start.set_defaults(func=main)

    one_shot = commands.add_parser("test-one-shot", help="Capture and publish one diagnostic pulse window.")
    one_shot.add_argument("--timeout-seconds", type=float, default=30.0)
    one_shot.set_defaults(func=main)

    test_flush = commands.add_parser("test-flush", help="Publish the current continuous diagnostic window.")
    test_flush.add_argument("--timeout-seconds", type=float, default=15.0)
    test_flush.set_defaults(func=main)

    test_stop = commands.add_parser("test-stop", help="Stop continuous diagnostic acquisition and clean its artifacts.")
    test_stop.add_argument("--timeout-seconds", type=float, default=30.0)
    test_stop.set_defaults(func=main)

    simulator_set = commands.add_parser("simulator-set", help="Set a remote acquisition simulator input.")
    simulator_set.add_argument("name", choices=("laser_enabled", "chopper_enabled", "pll_locked"))
    simulator_set.add_argument("state", choices=("on", "off"))
    simulator_set.add_argument("--timeout-seconds", type=float, default=15.0)
    simulator_set.set_defaults(func=main)

    simulator_restore = commands.add_parser("simulator-restore", help="Restore all remote acquisition simulator inputs.")
    simulator_restore.add_argument("--timeout-seconds", type=float, default=15.0)
    simulator_restore.set_defaults(func=main)


def _call_acquisition_event(event_name: bytes, payload: bytes, timeout_seconds: float) -> str:
    if timeout_seconds <= 0:
        raise ValueError("timeout must be positive.")
    configured = threading.Event()
    provider = []
    dds_client = client.DDSClient(uuid.uuid4(), ip=ECS_IP)

    def on_ready() -> None:
        handle = dds_client.register_subsystem(f"__acquisition_cli_{uuid.uuid4()}", uuid.uuid4(), temporary=True)
        provider.append(handle.add_event_provider(event_name))
        configured.set()

    dds_client.when_ready().then(on_ready)
    try:
        if not configured.wait(timeout=min(timeout_seconds, 10.0)):
            raise TimeoutError("Timed out connecting to the acquisition subsystem.")
        event_handle = provider[0].call(payload, [uuids.UUID_EUV_ACQUISITION_CONTROLLER])
        if event_handle is None:
            raise RuntimeError("Failed to send the acquisition control request.")
        deadline = time.monotonic() + timeout_seconds
        while event_handle.is_in_progress() and time.monotonic() < deadline:
            time.sleep(0.1)
        if event_handle.is_in_progress():
            raise TimeoutError("Timed out waiting for the acquisition subsystem.")
        state = event_handle.get_state(uuids.UUID_EUV_ACQUISITION_CONTROLLER)
        result = event_handle.get_result(uuids.UUID_EUV_ACQUISITION_CONTROLLER)
        message = bytes(result or b"").decode("utf-8", errors="replace")
        if state != client.EVENT_OK:
            raise RuntimeError(message or "Acquisition control request failed.")
        return message or "Acquisition control request completed."
    finally:
        dds_client.close()


def main(args: argparse.Namespace) -> int:
    if args.acquisition_command == "resume-interlock":
        print(_call_acquisition_event(b"resume_acquisition_interlock", bytes(), args.timeout_seconds))
        return 0
    if args.acquisition_command == "recover-orphan":
        if not args.confirm:
            raise ValueError("recover-orphan requires --confirm.")
        print(_call_acquisition_event(b"recover_orphaned_capture_session", b"confirm", args.timeout_seconds))
        return 0
    diagnostic_events = {
        "test-start": b"acquisition_test_start",
        "test-one-shot": b"acquisition_test_one_shot",
        "test-flush": b"acquisition_test_flush",
        "test-stop": b"acquisition_test_stop",
    }
    if args.acquisition_command in diagnostic_events:
        print(_call_acquisition_event(diagnostic_events[args.acquisition_command], bytes(), args.timeout_seconds))
        return 0
    if args.acquisition_command == "simulator-set":
        payload = json.dumps(
            {"name": args.name, "enabled": args.state == "on"},
            separators=(",", ":"),
        ).encode("utf-8")
        print(_call_acquisition_event(b"set_acquisition_simulator_control", payload, args.timeout_seconds))
        return 0
    if args.acquisition_command == "simulator-restore":
        print(_call_acquisition_event(b"restore_acquisition_simulator_controls", bytes(), args.timeout_seconds))
        return 0
    raise ValueError(f"Unknown acquisition command {args.acquisition_command!r}.")