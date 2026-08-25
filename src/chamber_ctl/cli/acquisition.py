from __future__ import annotations

import argparse
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
    raise ValueError(f"Unknown acquisition command {args.acquisition_command!r}.")