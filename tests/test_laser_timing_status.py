from chamber_ctl.subsystems.laser import DummyLaserSyncProvider, LaserSyncSubsystem


class _StoppedChopperProvider:
    def get_laser_on(self) -> bool:
        return True

    def get_laser_warming_up(self) -> bool:
        return False

    def get_chopper_on(self) -> bool:
        return False

    def get_chopper_starting_up(self) -> bool:
        return False

    def get_current_phase(self) -> float:
        return 0.0

    def get_chopper_frequency_hz(self) -> float:
        return 0.0


def test_dummy_laser_frequency_drives_neutral_timing_status_contract() -> None:
    provider = DummyLaserSyncProvider()
    provider.set_chopper_frequency_hz(180.0)

    assert provider.get_chopper_frequency_hz() == 180.0


def test_dummy_provider_status_separates_requested_and_physical_chopper_state() -> None:
    provider = DummyLaserSyncProvider(chopper_startup_time=60.0)
    provider.set_chopper_on(True)

    status = provider.get_hardware_status()

    assert status.desired_chopper_on is True
    assert status.chopper_on is True
    assert status.chopper_spinning is True
    assert status.target_chopper_frequency_hz == 192
    assert status.measured_chopper_frequency_hz == 192.0


def test_laser_timing_status_accepts_a_stopped_chopper() -> None:
    subsystem = object.__new__(LaserSyncSubsystem)
    subsystem._LaserSyncSubsystem__sync = _StoppedChopperProvider()
    subsystem._LaserSyncSubsystem__preinit_phase = 0.0
    subsystem._LaserSyncSubsystem__target_phase = 10.0

    timing = subsystem._LaserSyncSubsystem__get_timing_status()

    assert timing.chopper_frequency_hz == 0.0
    assert timing.triggers_enabled is False
    assert timing.euv_transmitting() is False