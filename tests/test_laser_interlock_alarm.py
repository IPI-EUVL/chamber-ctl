from dataclasses import replace
from types import SimpleNamespace
import uuid

import pytest

import chamber_ctl.subsystems.laser as laser_module
from chamber_ctl.subsystems.laser import LaserSyncSubsystem
from euv_acquisition.health import AcquisitionHealth
from ipi_ecs.dds.subsystem import StatusItem


class StatusPublisher:
    def __init__(self):
        self.items = {}

    def put_status_item(self, item):
        self.items[item.get_code()] = item

    def get_status_item_exists(self, code):
        return code in self.items

    def clear_status_item(self, code):
        self.items.pop(code, None)


class Provider:
    def __init__(self):
        self.result = (True, b"phase accepted")
        self.laser_on = True
        self.calls = []

    def set_target_phase(self, phase):
        self.calls.append(phase)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def get_laser_on(self):
        return self.laser_on

    def get_chopper_on(self):
        return True

    def get_laser_warming_up(self):
        return False

    def get_chopper_starting_up(self):
        return False

    def get_current_phase(self):
        return 10.0

    def get_target_phase(self):
        return 10.0


@pytest.fixture
def setup(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(laser_module, "time", SimpleNamespace(
        monotonic=lambda: clock[0], sleep=lambda _seconds: None,
    ))
    laser = object.__new__(LaserSyncSubsystem)
    publisher = StatusPublisher()
    logs = []
    jobs = []
    provider = Provider()
    health = AcquisitionHealth(True, uuid.uuid4(), 17, 0.305, True, False, False,
                               "Pulse reports stopped while laser timing expected triggers.")
    fields = {
        "subsystem": publisher, "status_item_cache": {},
        "logger": SimpleNamespace(log=lambda message, **data: logs.append({"message": message, **data})),
        "sync": provider, "experiment_active": True, "stop_handle": None,
        "acquisition_health": health, "acquisition_health_received_at": 99.95,
        "interlock_closed": False, "interlock_close_pending": False,
        "interlock_reopen_pending": False, "interlock_alarm_context": None,
        "interlock_alarm_state": None, "interlock_alarm_error": None,
        "preinit_phase": 0.0, "target_phase": 10.0,
        "target_status": SimpleNamespace(is_running=False),
        "enqueue_setter_job": lambda name, payload=None: jobs.append(name),
    }
    for key, value in fields.items():
        setattr(laser, f"_LaserSyncSubsystem__{key}", value)
    return SimpleNamespace(laser=laser, publisher=publisher, logs=logs, jobs=jobs,
                           provider=provider, health=health, clock=clock)


def update(env):
    env.laser._LaserSyncSubsystem__update_acquisition_interlock()


def close(env):
    env.laser._LaserSyncSubsystem__execute_interlock_close()


def alarm(env):
    item = env.publisher.items[105]
    assert item.get_severity() == StatusItem.STATE_ALARM
    return item.get_message()


@pytest.mark.parametrize("reason_code", ["pulse_loss", "stale_health"])
def test_close_request_and_completion_preserve_trigger_details(setup, reason_code):
    env = setup
    if reason_code == "stale_health":
        env.laser._LaserSyncSubsystem__acquisition_health = replace(env.health, pulse_loss=False, reason=None)
        env.laser._LaserSyncSubsystem__acquisition_health_received_at = 98.5
    update(env)
    assert env.jobs == ["interlock_close"]
    assert "Acquisition interlock" in alarm(env)
    assert "closure pending" in alarm(env)
    original_context = dict(env.laser._LaserSyncSubsystem__interlock_alarm_context)
    assert original_context["reason_code"] == reason_code
    assert original_context["last_sequence"] == 17
    assert original_context["session_id"] == str(env.health.session_id)
    assert original_context["last_pulse_age_seconds"] == 0.305
    if reason_code == "stale_health":
        assert "stale" in alarm(env)
        assert original_context["health_age_seconds"] == 1.5
    # A healthy update arriving before the queued closure cannot erase its cause.
    env.laser._LaserSyncSubsystem__on_acquisition_health(
        replace(env.health, last_sequence=999, pulse_loss=False, reason=None).encode()
    )
    close(env)
    assert "sample closed" in alarm(env)
    assert "sequence 17" in alarm(env)
    for event_name in ("acquisition_interlock_close_requested", "acquisition_interlock_close"):
        event = next(record for record in env.logs if record.get("event") == event_name)
        assert all(event[key] == value for key, value in original_context.items())


def test_healthy_updates_and_ready_provider_do_not_clear_closed_alarm(setup):
    env = setup
    update(env)
    close(env)
    original_message = alarm(env)
    env.laser._LaserSyncSubsystem__on_acquisition_health(
        replace(env.health, pulse_loss=False, reason=None, recovery_ready=True).encode()
    )
    update(env)
    env.laser._LaserSyncSubsystem__update_status_items()
    assert alarm(env) == original_message
    assert env.publisher.items[0].get_message() == "Acquisition interlock active"
    assert env.jobs == ["interlock_close"]


@pytest.mark.parametrize("failure", [(False, b"phase drive unavailable"), OSError("phase drive unavailable")])
def test_close_failure_stays_alarm_after_healthy_updates(setup, failure):
    env = setup
    env.provider.result = failure
    update(env)
    close(env)
    assert "closure FAILED" in alarm(env)
    assert "phase drive unavailable" in alarm(env)
    assert not env.laser._LaserSyncSubsystem__interlock_closed
    event = next(record for record in env.logs if record.get("event") == "acquisition_interlock_close_failed")
    assert event["reason_code"] == "pulse_loss"
    assert "phase drive unavailable" in event["provider_error"]
    env.laser._LaserSyncSubsystem__on_acquisition_health(replace(env.health, pulse_loss=False, reason=None).encode())
    update(env)
    assert "closure FAILED" in alarm(env)


def test_only_successful_authorized_reopen_clears_alarm(setup):
    env = setup
    update(env)
    close(env)
    recovered = replace(env.health, pulse_loss=False, reason=None, recovery_ready=True, resume_authorized=True)
    env.laser._LaserSyncSubsystem__on_acquisition_health(recovered.encode())
    update(env)
    assert env.jobs == ["interlock_close"]  # Target has not resumed yet.
    env.laser._LaserSyncSubsystem__target_status.is_running = True
    update(env)
    assert env.jobs[-1] == "interlock_reopen"
    assert "authorized sample reopening pending" in alarm(env)
    env.provider.result = (False, b"drive busy")
    env.laser._LaserSyncSubsystem__execute_interlock_reopen()
    assert "reopening FAILED" in alarm(env)
    assert env.laser._LaserSyncSubsystem__interlock_closed
    env.provider.result = (True, b"phase accepted")
    update(env)
    env.laser._LaserSyncSubsystem__execute_interlock_reopen()
    assert 105 not in env.publisher.items
    assert not env.laser._LaserSyncSubsystem__interlock_closed
    event = next(record for record in env.logs if record.get("event") == "acquisition_interlock_reopen")
    assert event["reason_code"] == "pulse_loss"
    assert event["last_sequence"] == 17


def test_completed_exposure_stop_clears_alarm_but_stop_request_does_not(setup):
    env = setup
    update(env)
    close(env)
    laser = env.laser
    for name in ("preinit_handle", "start_handle", "test_preinit_handle", "test_init_handle", "test_stop_handle"):
        setattr(laser, f"_LaserSyncSubsystem__{name}", None)
    laser._LaserSyncSubsystem__set_diagnostic_phase = lambda _phase: None
    laser._LaserSyncSubsystem__close_diagnostic_stream = lambda _outcome: None
    laser._LaserSyncSubsystem__run = True
    laser._LaserSyncSubsystem__status_publisher = None
    laser._on_did_stop = lambda _message: None
    laser._on_stop(object())
    assert 105 in env.publisher.items
    laser._LaserSyncSubsystem__stop_setup_pending = False
    env.provider.laser_on = False
    iterations = iter((True, False))
    laser._LaserSyncSubsystem__thread(SimpleNamespace(run=lambda: next(iterations)))
    assert 105 not in env.publisher.items
    assert not laser._LaserSyncSubsystem__experiment_active


def test_new_exposure_reset_clears_only_interlock_alarm(setup):
    env = setup
    update(env)
    close(env)
    env.publisher.items[102] = StatusItem(StatusItem.STATE_ALARM, 102, "Unrelated hardware fault")
    laser = env.laser
    laser._LaserSyncSubsystem__open_diagnostic_stream = lambda _run_id: None
    laser._LaserSyncSubsystem__pending_run_id = None
    accepted, _message = laser._on_preinit(object())
    assert accepted
    assert 105 not in env.publisher.items
    assert 102 in env.publisher.items
    assert laser._LaserSyncSubsystem__interlock_alarm_context is None


def test_healthy_status_does_not_create_alarm_or_move_shutter(setup):
    env = setup
    env.laser._LaserSyncSubsystem__on_acquisition_health(replace(env.health, pulse_loss=False, reason=None).encode())
    update(env)
    env.laser._LaserSyncSubsystem__update_status_items()
    assert 105 not in env.publisher.items
    assert env.jobs == []
    assert env.provider.calls == []
