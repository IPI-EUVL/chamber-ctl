import uuid

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