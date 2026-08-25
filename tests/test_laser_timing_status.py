from chamber_ctl.subsystems.laser import DummyLaserSyncProvider


def test_dummy_laser_frequency_drives_neutral_timing_status_contract() -> None:
    provider = DummyLaserSyncProvider()
    provider.set_chopper_frequency_hz(180.0)

    assert provider.get_chopper_frequency_hz() == 180.0