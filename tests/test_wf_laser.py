from __future__ import annotations

from collections import deque

from chamber_ctl.subsystems.wf_laser import WFLaserSyncProvider


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


class _FakeSerial:
    def __init__(self, frequency_hz: float = 0.0) -> None:
        self.frequency_hz = frequency_hz
        self.is_open = True
        self.writes: list[str] = []
        self._responses: deque[bytes] = deque()

    def write(self, payload: bytes) -> int:
        command = payload.decode("utf-8").strip()
        self.writes.append(command)
        self._responses.append(f"{command}\r".encode("utf-8"))
        if command == "refoutfreq?":
            self._responses.append(f"{self.frequency_hz}\r".encode("utf-8"))
        return len(payload)

    def read_until(self, _expected: bytes) -> bytes:
        return self._responses.popleft() if self._responses else b""

    def flush(self) -> None:
        return None

    def close(self) -> None:
        self.is_open = False


class _FakeWaveform:
    def __init__(self) -> None:
        self.writes: list[str] = []
        self.timeout = None
        self.write_termination = None
        self.read_termination = None
        self.closed = False

    def write(self, command: str) -> None:
        self.writes.append(command)

    def query(self, _command: str) -> str:
        return "waveform"

    def close(self) -> None:
        self.closed = True


class _FakeVisaManager:
    def __init__(self, waveform: _FakeWaveform) -> None:
        self.waveform = waveform
        self.resources: list[str] = []

    def open_resource(self, resource: str) -> _FakeWaveform:
        self.resources.append(resource)
        return self.waveform


def _provider(clock: _Clock, serial_device: _FakeSerial, waveform: _FakeWaveform, **kwargs) -> WFLaserSyncProvider:
    return WFLaserSyncProvider(
        serial_factory=lambda *_args, **_kwargs: serial_device,
        visa_resource_manager_factory=lambda: _FakeVisaManager(waveform),
        monotonic=clock,
        **kwargs,
    )


def _start_ready_chopper(provider: WFLaserSyncProvider, serial_device: _FakeSerial) -> None:
    assert provider.set_chopper_frequency_hz(192.0)[0] is True
    assert provider.set_chopper_on(True)[0] is True
    serial_device.frequency_hz = 192.0
    provider.refresh_hardware_status()


def test_chopper_ready_means_measured_frequency_is_within_target_tolerance() -> None:
    clock = _Clock()
    serial_device = _FakeSerial()
    provider = _provider(clock, serial_device, _FakeWaveform())

    _start_ready_chopper(provider, serial_device)
    status = provider.get_hardware_status()

    assert "freq=192" in serial_device.writes
    assert "enable=1" in serial_device.writes
    assert status.desired_chopper_on is True
    assert status.chopper_on is True
    assert status.chopper_spinning is True
    assert status.target_chopper_frequency_hz == 192
    assert status.measured_chopper_frequency_hz == 192.0

    serial_device.frequency_hz = 194.1
    clock.value += 1.0
    status = provider.refresh_hardware_status()

    assert status.chopper_spinning is True
    assert status.chopper_on is False


def test_laser_requires_chopper_at_target_and_shuts_down_when_it_faults() -> None:
    clock = _Clock()
    serial_device = _FakeSerial()
    waveform = _FakeWaveform()
    provider = _provider(clock, serial_device, waveform)

    assert provider.set_laser_on(True)[0] is False
    assert waveform.writes == []

    _start_ready_chopper(provider, serial_device)
    assert provider.set_laser_on(True)[0] is True
    assert waveform.writes == ["OUTPut ON"]

    serial_device.frequency_hz = 0.0
    clock.value += 1.0
    status = provider.refresh_hardware_status()

    assert status.laser_on is False
    assert status.desired_laser_on is False
    assert "OUTPut OFF" in waveform.writes
    assert "Chopper left its target" in status.chopper_error


def test_chopper_recovery_is_rate_limited_and_latches_an_error() -> None:
    clock = _Clock()
    serial_device = _FakeSerial(0.0)
    provider = _provider(
        clock,
        serial_device,
        _FakeWaveform(),
        recovery_max_attempts=3,
        recovery_window_seconds=60.0,
        recovery_delay_seconds=5.0,
    )

    assert provider.set_chopper_frequency_hz(192.0)[0] is True
    assert provider.set_chopper_on(True)[0] is True
    for _ in range(3):
        clock.value += 5.0
        status = provider.refresh_hardware_status()

    assert status.chopper_recovery_exhausted is True
    assert status.chopper_on is False
    assert serial_device.writes.count("enable=1") == 3
    assert "Automatic chopper recovery exhausted" in status.chopper_error


def test_chopper_reconnect_reapplies_target_frequency() -> None:
    clock = _Clock()
    serial_device = _FakeSerial(192.0)
    waveform = _FakeWaveform()
    attempts = [0]

    def serial_factory(*_args, **_kwargs):
        attempts[0] += 1
        if attempts[0] == 1:
            raise OSError("controller unplugged")
        return serial_device

    provider = WFLaserSyncProvider(
        serial_factory=serial_factory,
        visa_resource_manager_factory=lambda: _FakeVisaManager(waveform),
        monotonic=clock,
        reconnect_interval_seconds=2.0,
    )

    assert provider.set_chopper_frequency_hz(192.0)[0] is True
    assert provider.get_hardware_status().chopper_connected is False

    clock.value = 2.0
    status = provider.refresh_hardware_status()

    assert attempts[0] == 2
    assert status.chopper_connected is True
    assert "freq=192" in serial_device.writes


def test_chopper_enable_stays_pending_when_controller_is_temporarily_disconnected() -> None:
    clock = _Clock()
    waveform = _FakeWaveform()

    provider = WFLaserSyncProvider(
        serial_factory=lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("controller unplugged")),
        visa_resource_manager_factory=lambda: _FakeVisaManager(waveform),
        monotonic=clock,
    )

    assert provider.set_chopper_frequency_hz(192.0)[0] is True
    ok, message = provider.set_chopper_on(True)
    status = provider.get_hardware_status()

    assert ok is True
    assert "pending" in message
    assert status.desired_chopper_on is True
    assert status.chopper_connected is False