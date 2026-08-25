from __future__ import annotations

import uuid

import pytest

from chamber_ctl.subsystems.batch_controller import (
    BatchControllerSubsystem,
    decode_batch_command,
    encode_batch_command,
)
from chamber_ctl.subsystems.batch_storage import BatchManifestLoadError
from chamber_ctl.subsystems.batcher import (
    BatchPlan,
    BatchPlanEntry,
    ControllerSnapshot,
    ExecutionMode,
    ExposureTemplate,
    ManifestStatus,
    TargetMode,
    batch_plan_to_dict,
)


class _Repository:
    def __init__(self):
        self.items = {}

    def create(self, manifest):
        self.items[manifest.batch_uuid] = manifest

    def save_control_state(self, manifest):
        self.items[manifest.batch_uuid] = manifest

    def save_revision(self, manifest, *, expected_revision):
        if self.items[manifest.batch_uuid].revision != expected_revision:
            raise ValueError("revision conflict")
        self.items[manifest.batch_uuid] = manifest


class _History:
    def load_attempts(self, _batch_uuid):
        return ()

    def repair_metrics(self, _run_uuid):
        raise AssertionError("No repair expected")


class _Controller:
    def __init__(self):
        self.state = ControllerSnapshot.stopped()
        self.starts = []
        self.lease_owned = False

    def snapshot(self):
        return self.state

    def acquire_automation(self, _owner_name):
        self.lease_owned = True
        return "acquired"

    def release_automation(self):
        self.lease_owned = False
        return "released"

    def start_exposure(self, settings, batch_uuid, run_tags=None):
        self.starts.append((settings, batch_uuid, run_tags))
        self.state = ControllerSnapshot.active(uuid.uuid4())
        return "started"


class _Logger:
    def log(self, *_args, **_kwargs):
        return None


def _plan():
    return BatchPlan(
        ExposureTemplate("Managed batch", operator="Operator"),
        (BatchPlanEntry(0, TargetMode.DOSE, 10.0),),
    )


def _subsystem():
    subsystem = object.__new__(BatchControllerSubsystem)
    subsystem._repository = _Repository()
    subsystem._history = _History()
    subsystem._controller = _Controller()
    subsystem._coordinator = None
    subsystem._manifests = {}
    subsystem._manifest_load_errors = ()
    subsystem._active = None
    subsystem._display_manifest = None
    subsystem._assessment = None
    subsystem._attempts = ()
    subsystem._lease_owned = False
    subsystem._execution_mode = ExecutionMode.MANUAL
    subsystem._phase = "idle"
    subsystem._message = ""
    subsystem._last_error = ""
    subsystem._next_poll = 0.0
    subsystem._logger = _Logger()
    return subsystem


def _create_and_activate(subsystem, mode):
    created = subsystem._execute_command(
        uuid.uuid4(),
        "create",
        {"plan": batch_plan_to_dict(_plan()), "mode": mode.value},
    )
    batch_uuid = created["batch_uuid"]
    subsystem._execute_command(uuid.uuid4(), "activate", {"batch_uuid": batch_uuid})
    return uuid.UUID(batch_uuid)


def test_command_codec_is_versioned_and_strict() -> None:
    assert decode_batch_command(encode_batch_command("pause", {"reason": "operator"})) == (
        "pause",
        {"reason": "operator"},
    )
    with pytest.raises(ValueError, match="only"):
        decode_batch_command(b'{"schema_version":1,"command":"pause","args":{},"extra":1}')


def test_manual_activation_waits_for_continue_and_consumes_one_start() -> None:
    subsystem = _subsystem()
    batch_uuid = _create_and_activate(subsystem, ExecutionMode.MANUAL)

    assert subsystem._phase == "waiting_continue"
    assert subsystem._controller.starts == []

    subsystem._execute_command(uuid.uuid4(), "continue", {})

    assert subsystem._phase == "starting"
    assert len(subsystem._controller.starts) == 1
    settings, started_batch_uuid, run_tags = subsystem._controller.starts[0]
    assert settings.get_sample() == "0"
    assert started_batch_uuid == batch_uuid
    assert run_tags == {"batch_revision": 1}


def test_automatic_activation_starts_immediately() -> None:
    subsystem = _subsystem()

    subsystem._execute_command(uuid.uuid4(), "set_mode", {"mode": ExecutionMode.AUTOMATIC.value})
    _create_and_activate(subsystem, ExecutionMode.MANUAL)

    assert len(subsystem._controller.starts) == 1
    assert subsystem._phase == "starting"


def test_execution_mode_is_controller_owned_and_switches_the_active_batch() -> None:
    subsystem = _subsystem()
    batch_uuid = _create_and_activate(subsystem, ExecutionMode.MANUAL)

    result = subsystem._execute_command(
        uuid.uuid4(),
        "set_mode",
        {"mode": ExecutionMode.AUTOMATIC.value},
    )

    assert result == {"mode": ExecutionMode.AUTOMATIC.value}
    assert subsystem._execution_mode is ExecutionMode.AUTOMATIC
    assert subsystem._manifests[batch_uuid].mode is ExecutionMode.MANUAL
    assert len(subsystem._controller.starts) == 1


def test_active_plan_edit_is_rejected_while_exposure_is_running() -> None:
    subsystem = _subsystem()
    batch_uuid = _create_and_activate(subsystem, ExecutionMode.MANUAL)
    subsystem._controller.state = ControllerSnapshot.active(uuid.uuid4())
    manifest = subsystem._manifests[batch_uuid]

    with pytest.raises(ValueError, match="cannot be edited"):
        subsystem._execute_command(
            uuid.uuid4(),
            "update",
            {
                "batch_uuid": str(batch_uuid),
                "expected_revision": manifest.revision,
                "plan": batch_plan_to_dict(_plan()),
            },
        )


def test_cancel_does_not_stop_active_exposure() -> None:
    subsystem = _subsystem()
    batch_uuid = _create_and_activate(subsystem, ExecutionMode.MANUAL)
    subsystem._controller.state = ControllerSnapshot.active(uuid.uuid4())

    subsystem._execute_command(uuid.uuid4(), "cancel", {})

    assert subsystem._manifests[batch_uuid].status is ManifestStatus.ACTIVE
    assert subsystem._manifests[batch_uuid].cancel_pending is True
    assert subsystem._phase == "cancelling"


def test_cancelled_manifest_can_be_activated_again() -> None:
    subsystem = _subsystem()
    batch_uuid = _create_and_activate(subsystem, ExecutionMode.MANUAL)

    subsystem._execute_command(uuid.uuid4(), "cancel", {})

    assert subsystem._manifests[batch_uuid].status is ManifestStatus.CANCELLED
    assert subsystem._active is None

    subsystem._execute_command(uuid.uuid4(), "activate", {"batch_uuid": str(batch_uuid)})

    assert subsystem._manifests[batch_uuid].status is ManifestStatus.ACTIVE
    assert subsystem._active is not None


def test_withdrawn_manifest_can_be_restored_to_draft() -> None:
    subsystem = _subsystem()
    created = subsystem._execute_command(
        uuid.uuid4(),
        "create",
        {"plan": batch_plan_to_dict(_plan()), "mode": ExecutionMode.MANUAL.value},
    )
    batch_uuid = created["batch_uuid"]

    subsystem._execute_command(uuid.uuid4(), "withdraw", {"batch_uuid": batch_uuid})
    restored = subsystem._execute_command(uuid.uuid4(), "unwithdraw", {"batch_uuid": batch_uuid})

    assert restored["status"] == ManifestStatus.DRAFT.value
    assert subsystem._manifests[uuid.UUID(batch_uuid)].status is ManifestStatus.DRAFT


def test_activation_is_blocked_when_a_malformed_manifest_is_marked_active() -> None:
    subsystem = _subsystem()
    created = subsystem._execute_command(
        uuid.uuid4(),
        "create",
        {"plan": batch_plan_to_dict(_plan()), "mode": ExecutionMode.MANUAL.value},
    )
    subsystem._manifest_load_errors = (
        BatchManifestLoadError(uuid.uuid4(), "missing", ManifestStatus.ACTIVE.value, "invalid manifest"),
    )

    with pytest.raises(ValueError, match="unreadable active manifest"):
        subsystem._execute_command(uuid.uuid4(), "activate", {"batch_uuid": created["batch_uuid"]})