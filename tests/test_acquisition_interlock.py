import threading
import time
import uuid

from chamber_ctl.subsystems.laser import LaserSyncStatus, LaserSyncSubsystem
from chamber_ctl.subsystems.target_controller import TargetController
from euv_acquisition.health import AcquisitionHealth


class _Logger:
    def __init__(self):
        self.messages = []

    def log(self, message, **kwargs):
        self.messages.append((message, kwargs))


class _Motion:
    def __init__(self, running=True):
        self.running = running
        self.stops = 0
        self.continues = 0

    def is_running(self):
        return self.running

    def stop(self):
        self.stops += 1
        self.running = False

    def continue_move(self):
        self.continues += 1
        return True


def _health(*, pulse_loss=False, recovery_ready=False, resume_authorized=False):
    return AcquisitionHealth(True, uuid.uuid4(), 3, 0.3, pulse_loss, recovery_ready, resume_authorized)


def test_target_pauses_only_after_laser_reports_sample_closed_and_resumes_before_reopen() -> None:
    controller = object.__new__(TargetController)
    motion = _Motion()
    controller._TargetController__motion_controller = motion
    controller._TargetController__logger = _Logger()
    controller._TargetController__interlock_paused = False
    controller._TargetController__interlock_resume_pending = False
    controller._TargetController__acquisition_health = _health(pulse_loss=True)
    controller._TargetController__acquisition_health_received_at = time.monotonic()
    controller._TargetController__laser_status = LaserSyncStatus(True, False, True, False, 0.0, 0.0, 0.0, 10.0)

    controller._TargetController__update_acquisition_interlock()

    assert motion.stops == 1
    assert controller._TargetController__interlock_paused is True
    assert controller._on_continue_state()[0] is True

    controller._TargetController__acquisition_health = _health(recovery_ready=True, resume_authorized=True)
    controller._TargetController__acquisition_health_received_at = time.monotonic()
    controller._TargetController__update_acquisition_interlock()
    assert motion.continues == 1
    assert controller._TargetController__interlock_resume_pending is True

    motion.running = True
    controller._TargetController__update_acquisition_interlock()
    assert controller._TargetController__interlock_paused is False
    assert controller._TargetController__interlock_resume_pending is False


def test_target_ignores_stale_health_for_motion_pause() -> None:
    controller = object.__new__(TargetController)
    motion = _Motion()
    controller._TargetController__motion_controller = motion
    controller._TargetController__logger = _Logger()
    controller._TargetController__interlock_paused = False
    controller._TargetController__interlock_resume_pending = False
    controller._TargetController__acquisition_health = _health(pulse_loss=True)
    controller._TargetController__acquisition_health_received_at = time.monotonic() - 1.1
    controller._TargetController__laser_status = LaserSyncStatus(True, False, True, False, 0.0, 0.0, 0.0, 10.0)

    controller._TargetController__update_acquisition_interlock()

    assert motion.stops == 0


def test_laser_enqueues_close_for_pulse_loss_and_stale_health() -> None:
    subsystem = object.__new__(LaserSyncSubsystem)
    jobs = []
    subsystem._LaserSyncSubsystem__experiment_active = True
    subsystem._LaserSyncSubsystem__stop_handle = None
    subsystem._LaserSyncSubsystem__acquisition_health = _health(pulse_loss=True)
    subsystem._LaserSyncSubsystem__acquisition_health_received_at = time.monotonic()
    subsystem._LaserSyncSubsystem__interlock_closed = False
    subsystem._LaserSyncSubsystem__interlock_close_pending = False
    subsystem._LaserSyncSubsystem__interlock_reopen_pending = False
    subsystem._LaserSyncSubsystem__enqueue_setter_job = lambda name: jobs.append(name)

    subsystem._LaserSyncSubsystem__update_acquisition_interlock()

    assert jobs == ["interlock_close"]