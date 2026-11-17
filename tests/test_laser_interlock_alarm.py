from dataclasses import replace
from types import SimpleNamespace
import uuid

import pytest

import chamber_ctl.subsystems.laser as laser_module
from chamber_ctl.subsystems import uuids
from chamber_ctl.subsystems.laser import LaserSyncSubsystem
from euv_acquisition.health import AcquisitionHealth
from ipi_ecs.dds import magics
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
        self.result = (True, b"disable command accepted")
        self.laser_on = True
        self.calls = []

    def set_laser_on(self, on):
        self.calls.append(on)
        if isinstance(self.result, Exception):
            raise self.result
        if self.result[0]:
            self.laser_on = on
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


class StopEvent:
    def __init__(self):
        self.calls = []
        self.failure = None
        self.aborted = 0
        self.request = SimpleNamespace(is_in_progress=lambda: True,
                                       get_state=lambda _uuid: magics.EVENT_IN_PROGRESS,
                                       abort=self.abort)

    def abort(self):
        self.aborted += 1

    def call(self, reason, targets):
        self.calls.append((reason, targets))
        if self.failure is not None:
            raise self.failure
        return self.request


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
    stop = StopEvent()
    health = AcquisitionHealth(True, uuid.uuid4(), 17, 0.305, True, False, False,
                               "Pulse reports stopped while laser timing expected triggers.")
    fields = {
        "subsystem": publisher, "status_item_cache": {},
        "logger": SimpleNamespace(log=lambda message, **data: logs.append({"message": message, **data})),
        "sync": provider, "experiment_active": True, "stop_handle": None,
        "acquisition_health": health, "acquisition_health_received_at": 99.95,
        "watchdog_alarm_context": None, "watchdog_alarm_state": None,
        "watchdog_alarm_error": None, "watchdog_stop_event": stop,
        "watchdog_stop_request": None, "watchdog_stop_error": None,
        "watchdog_next_stop_attempt": 0.0, "watchdog_stop_requested_at": 0.0,
        "watchdog_shutdown_pending": False,
        "watchdog_shutdown_ok": False, "watchdog_next_shutdown_attempt": 0.0,
        "preinit_phase": 0.0, "target_phase": 10.0,
        "target_status": SimpleNamespace(is_running=False),
        "enqueue_setter_job": lambda name, payload=None: jobs.append(name),
    }
    for key, value in fields.items():
        setattr(laser, f"_LaserSyncSubsystem__{key}", value)
    return SimpleNamespace(laser=laser, publisher=publisher, logs=logs, jobs=jobs,
                           provider=provider, stop=stop, health=health, clock=clock)


def update(env):
    env.laser._LaserSyncSubsystem__update_acquisition_watchdog()


def disable(env):
    env.laser._LaserSyncSubsystem__execute_watchdog_shutdown()


def alarm(env):
    item = env.publisher.items[105]
    assert item.get_severity() == StatusItem.STATE_ALARM
    return item.get_message()


@pytest.mark.parametrize("reason_code", ["pulse_loss", "stale_health"])
def test_watchdog_disables_trigger_and_requests_whole_exposure_stop(setup, reason_code):
    env = setup
    if reason_code == "stale_health":
        env.laser._LaserSyncSubsystem__acquisition_health = replace(env.health, pulse_loss=False, reason=None)
        env.laser._LaserSyncSubsystem__acquisition_health_received_at = 98.5
    update(env)
    assert env.jobs == ["watchdog_shutdown"]
    assert "Acquisition watchdog" in alarm(env)
    assert "disable pending" in alarm(env)
    context = dict(env.laser._LaserSyncSubsystem__watchdog_alarm_context)
    assert context["reason_code"] == reason_code
    assert context["last_sequence"] == 17
    assert context["session_id"] == str(env.health.session_id)
    assert context["last_pulse_age_seconds"] == 0.305
    assert len(env.stop.calls) == 1
    reason, targets = env.stop.calls[0]
    assert targets == [uuids.UUID_EXPOSURE_CONTROLLER]
    assert context["reason"] in reason.decode()
    env.laser._LaserSyncSubsystem__on_acquisition_health(
        replace(env.health, last_sequence=999, pulse_loss=False, reason=None).encode()
    )
    disable(env)
    assert env.provider.calls == [False]
    assert "disable command accepted" in alarm(env)
    assert "sequence 17" in alarm(env)
    assert "closed" not in alarm(env)
    for name in ("acquisition_watchdog_tripped", "acquisition_watchdog_stop_requested",
                 "acquisition_watchdog_disable_accepted"):
        event = next(record for record in env.logs if record.get("event") == name)
        assert all(event[key] == value for key, value in context.items())


def test_recovery_cannot_resume_a_tripped_exposure_or_clear_alarm(setup):
    env = setup
    update(env)
    disable(env)
    original = alarm(env)
    env.laser._LaserSyncSubsystem__on_acquisition_health(
        replace(env.health, pulse_loss=False, reason=None, recovery_ready=True, resume_authorized=True).encode()
    )
    env.laser._LaserSyncSubsystem__target_status.is_running = True
    update(env)
    env.laser._LaserSyncSubsystem__update_status_items()
    assert alarm(env) == original
    assert env.publisher.items[0].get_message() == "Acquisition watchdog active"
    assert env.jobs == ["watchdog_shutdown"]
    assert env.provider.calls == [False]
    assert len(env.stop.calls) == 1


@pytest.mark.parametrize("failure", [(False, b"output unavailable"), OSError("output unavailable")])
def test_output_disable_failure_is_latched_and_retried_independently(setup, failure):
    env = setup
    env.provider.result = failure
    update(env)
    disable(env)
    assert "disable FAILED" in alarm(env)
    assert "output unavailable" in alarm(env)
    env.laser._LaserSyncSubsystem__on_acquisition_health(replace(env.health, pulse_loss=False, reason=None).encode())
    update(env)
    assert env.jobs == ["watchdog_shutdown"]
    env.clock[0] += 1.1
    update(env)
    assert env.jobs == ["watchdog_shutdown", "watchdog_shutdown"]
    assert len(env.stop.calls) == 1  # The coordinated request is still in progress.
    env.provider.result = (True, "off accepted")
    disable(env)
    assert "disable command accepted" in alarm(env)


def test_failed_coordinator_call_does_not_prevent_output_disable_and_retries(setup):
    env = setup
    env.stop.failure = ConnectionError("DDS unavailable")
    update(env)
    disable(env)
    assert env.provider.calls == [False]
    assert "DDS unavailable" in alarm(env)
    update(env)
    assert len(env.stop.calls) == 1
    env.clock[0] += 1.1
    env.stop.failure = None
    update(env)
    assert len(env.stop.calls) == 2
    assert "DDS unavailable" not in alarm(env)


def test_failed_delivery_retries_but_pending_or_successful_request_does_not(setup):
    env = setup
    update(env)
    disable(env)
    env.clock[0] += 4.0
    update(env)
    assert len(env.stop.calls) == 1
    env.stop.request.is_in_progress = lambda: False
    env.stop.request.get_state = lambda _uuid: magics.EVENT_REJ
    update(env)
    assert "delivery failed" in alarm(env)
    env.clock[0] += 1.1
    update(env)
    assert len(env.stop.calls) == 2
    env.stop.request.get_state = lambda _uuid: magics.EVENT_OK
    env.clock[0] += 5.0
    update(env)
    assert len(env.stop.calls) == 2


def test_normal_stop_receipt_suppresses_duplicate_stop_request_and_keeps_alarm(setup):
    env = setup
    update(env)
    disable(env)
    laser = env.laser
    for name in ("preinit_handle", "start_handle", "test_preinit_handle", "test_init_handle", "test_stop_handle"):
        setattr(laser, f"_LaserSyncSubsystem__{name}", None)
    laser._LaserSyncSubsystem__set_diagnostic_phase = lambda _phase: None
    laser._LaserSyncSubsystem__close_diagnostic_stream = lambda _outcome: None
    laser._LaserSyncSubsystem__run = True
    laser._LaserSyncSubsystem__status_publisher = None
    laser._on_did_stop = lambda _message: None
    laser._on_stop(object())
    update(env)
    assert len(env.stop.calls) == 1
    laser._LaserSyncSubsystem__stop_setup_pending = False
    env.provider.laser_on = False
    iterations = iter((True, False))
    laser._LaserSyncSubsystem__thread(SimpleNamespace(run=lambda: next(iterations)))
    assert 105 in env.publisher.items
    assert not laser._LaserSyncSubsystem__experiment_active


def test_new_exposure_reset_clears_only_watchdog_alarm(setup):
    env = setup
    update(env)
    disable(env)
    env.publisher.items[102] = StatusItem(StatusItem.STATE_ALARM, 102, "Unrelated hardware fault")
    laser = env.laser
    laser._LaserSyncSubsystem__open_diagnostic_stream = lambda _run_id: None
    laser._LaserSyncSubsystem__pending_run_id = None
    accepted, _message = laser._on_preinit(object())
    assert accepted
    assert 105 not in env.publisher.items
    assert 102 in env.publisher.items
    assert laser._LaserSyncSubsystem__watchdog_alarm_context is None
    assert laser._LaserSyncSubsystem__watchdog_stop_request is None


def test_queued_preinit_cannot_reenable_output_after_watchdog_trip(setup):
    env = setup
    update(env)
    disable(env)
    env.laser._LaserSyncSubsystem__preinit_handle = object()
    env.laser._LaserSyncSubsystem__preinit_setup_pending = True
    env.laser._LaserSyncSubsystem__execute_exp_preinit_laser_setters()
    assert env.provider.calls == [False]


def test_stale_health_uses_coordinated_stop_not_immediate_abort(setup):
    env = setup
    env.laser._LaserSyncSubsystem__acquisition_health_received_at = 95.0
    assert env.laser._on_continue_state()[0] is True
    update(env)
    assert len(env.stop.calls) == 1


def test_healthy_status_does_not_create_alarm_or_issue_commands(setup):
    env = setup
    env.laser._LaserSyncSubsystem__on_acquisition_health(replace(env.health, pulse_loss=False, reason=None).encode())
    update(env)
    env.laser._LaserSyncSubsystem__update_status_items()
    assert 105 not in env.publisher.items
    assert env.jobs == []
    assert env.provider.calls == []
    assert env.stop.calls == []


def test_queued_start_cannot_move_phase_after_watchdog_trip(setup):
    env = setup
    update(env)
    disable(env)
    env.laser._LaserSyncSubsystem__start_handle = object()
    env.laser._LaserSyncSubsystem__start_setup_pending = True
    env.laser._LaserSyncSubsystem__execute_exp_start_setters()
    assert env.provider.calls == [False]
    assert not env.laser._LaserSyncSubsystem__start_setup_pending


def test_pending_stop_request_expires_then_retries_with_alarm(setup):
    env = setup
    update(env)
    disable(env)
    env.clock[0] += 5.1
    update(env)
    assert env.stop.aborted == 1
    assert len(env.stop.calls) == 1
    assert "delivery timed out" in alarm(env)
    env.clock[0] += 1.1
    update(env)
    assert len(env.stop.calls) == 2
    assert "delivery timed out" not in alarm(env)
    assert env.provider.calls == [False]


def test_normal_stop_callback_prevents_pending_timeout_retry(setup):
    env = setup
    update(env)
    disable(env)
    env.laser._LaserSyncSubsystem__stop_handle = object()
    env.clock[0] += 10.0
    update(env)
    assert env.stop.aborted == 0
    assert len(env.stop.calls) == 1
    assert "delivery timed out" not in alarm(env)


def test_new_exposure_does_not_reuse_previous_fault_but_requires_fresh_health(setup):
    env = setup
    update(env)
    disable(env)
    laser = env.laser
    laser._LaserSyncSubsystem__open_diagnostic_stream = lambda _run_id: None
    laser._LaserSyncSubsystem__pending_run_id = None
    laser._on_preinit(object())
    assert laser._LaserSyncSubsystem__acquisition_health is None
    env.clock[0] += 0.05
    update(env)
    assert laser._LaserSyncSubsystem__watchdog_alarm_context is None
    assert len(env.stop.calls) == 1  # Only the prior exposure's request.
    env.clock[0] += 1.1
    update(env)
    assert laser._LaserSyncSubsystem__watchdog_alarm_context["reason_code"] == "stale_health"
    assert laser._LaserSyncSubsystem__watchdog_alarm_context["session_id"] is None
    assert len(env.stop.calls) == 2
