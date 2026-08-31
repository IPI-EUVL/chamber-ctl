from types import SimpleNamespace

from ipi_ecs.dds import magics

from chamber_ctl.subsystems.exposure_controller import ExposureSettings
from chamber_ctl.subsystems.laser import DummyLaserSyncProvider, LaserSyncStatus, LaserSyncSubsystem


def test_dummy_laser_provider_persists_requested_chopper_frequency() -> None:
    provider = DummyLaserSyncProvider()

    assert provider.set_chopper_frequency_hz(180.0)[0] is True
    assert provider.get_chopper_frequency_hz() == 180.0
    assert provider.set_chopper_frequency_hz(0.0)[0] is False


def test_laser_status_carries_optional_measured_frequency() -> None:
    status = LaserSyncStatus(True, False, True, False, 1.0, 1.0, 0.0, 1.0, 192.0)

    assert LaserSyncStatus.decode(status.encode()).chopper_frequency_hz == 192.0


def test_laser_preinit_rejects_missing_or_invalid_chopper_frequency() -> None:
    subsystem = object.__new__(LaserSyncSubsystem)
    subsystem._LaserSyncSubsystem__requested_chopper_frequency_hz = None
    subsystem._LaserSyncSubsystem__test_active = False
    subsystem._LaserSyncSubsystem__test_in_progress = lambda: False
    subsystem._LaserSyncSubsystem__exposure_control_in_progress = lambda: False

    missing, _reason = subsystem._can_preinit(ExposureSettings(chopper_frequency_hz=None), None)
    valid, _reason = subsystem._can_preinit(ExposureSettings(chopper_frequency_hz=180.0), None)

    assert missing is False
    assert valid is True
    assert subsystem._LaserSyncSubsystem__requested_chopper_frequency_hz == 180.0


def test_manual_chopper_target_is_sent_to_provider_only_while_exposure_is_idle() -> None:
    class _Provider:
        def __init__(self) -> None:
            self.targets = []

        def set_chopper_frequency_hz(self, frequency_hz: float):
            self.targets.append(frequency_hz)
            return True, f"Chopper target frequency set to {frequency_hz:.0f} Hz."

        def get_hardware_status(self):
            target = None if not self.targets else int(self.targets[-1])
            return SimpleNamespace(target_chopper_frequency_hz=target)

    provider = _Provider()
    subsystem = object.__new__(LaserSyncSubsystem)
    subsystem._LaserSyncSubsystem__sync = provider
    subsystem._LaserSyncSubsystem__experiment_active = False
    subsystem._LaserSyncSubsystem__preinit_handle = None
    subsystem._LaserSyncSubsystem__start_handle = None
    subsystem._LaserSyncSubsystem__stop_handle = None
    subsystem._LaserSyncSubsystem__logger = SimpleNamespace(log=lambda *_args, **_kwargs: None)

    state, _result = subsystem._LaserSyncSubsystem__on_chopper_frequency_write(None, None, 190.0)

    assert state == magics.TRANSOP_STATE_OK
    assert provider.targets == [190.0]
    assert subsystem._LaserSyncSubsystem__on_chopper_frequency_read(None) == (magics.TRANSOP_STATE_OK, 190.0)

    subsystem._LaserSyncSubsystem__experiment_active = True
    state, reason = subsystem._LaserSyncSubsystem__on_chopper_frequency_write(None, None, 192.0)

    assert state == magics.TRANSOP_STATE_REJ
    assert b"active exposure" in reason
    assert provider.targets == [190.0]