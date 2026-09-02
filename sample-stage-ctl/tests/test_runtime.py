from __future__ import annotations

import importlib
import threading
import time
from dataclasses import dataclass, field

from sample_stage_ctl.controller import ENABLE_PIN
from sample_stage_ctl.main import HardwareDependencies, create_runtime
from sample_stage_ctl.protocol import SERVER_INCOMING_HOST, SERVER_INCOMING_PORT


@dataclass
class FakeGPIO:
    BCM: str = "BCM"
    OUT: str = "OUT"
    IN: str = "IN"
    HIGH: int = 1
    LOW: int = 0
    PUD_UP: str = "PUD_UP"
    calls: list[tuple[object, ...]] = field(default_factory=list)

    def setmode(self, mode: object) -> None:
        self.calls.append(("setmode", mode))

    def setup(
        self,
        channel: int,
        mode: object,
        pull_up_down: object | None = None,
    ) -> None:
        self.calls.append(("setup", channel, mode, pull_up_down))

    def output(self, channel: int, value: object) -> None:
        self.calls.append(("output", channel, value))

    def input(self, _channel: int) -> object:
        return self.HIGH

    def cleanup(self) -> None:
        self.calls.append(("cleanup",))


class FakeI2C:
    def __init__(self) -> None:
        self.deinitialized = False

    def deinit(self) -> None:
        self.deinitialized = True


class FakePin:
    value = False

    def switch_to_input(self, *, pull: object) -> None:
        self.pull = pull


class FakeExpander:
    def __init__(self, _i2c: object, *, address: int) -> None:
        self.address = address

    def get_pin(self, _pin_number: int) -> FakePin:
        return FakePin()


class FakeSocket:
    def __init__(self) -> None:
        self.bound: tuple[str, int] | None = None
        self.timeout: float | None = None
        self.closed = False
        self.sent: list[tuple[bytes, tuple[str, int]]] = []

    def bind(self, address: tuple[str, int]) -> None:
        self.bound = address

    def settimeout(self, value: float) -> None:
        self.timeout = value

    def recvfrom(self, _buffer_size: int) -> tuple[bytes, tuple[str, int]]:
        if self.closed:
            raise OSError("closed")
        time.sleep(0.001)
        raise TimeoutError

    def sendto(self, data: bytes, address: tuple[str, int]) -> int:
        self.sent.append((data, address))
        return len(data)

    def close(self) -> None:
        self.closed = True


def test_imports_do_not_load_raspberry_pi_modules() -> None:
    importlib.import_module("sample_stage_ctl")
    importlib.import_module("sample_stage_ctl.main")


def test_runtime_disables_drivers_before_workers_start_and_cleans_up() -> None:
    gpio = FakeGPIO()
    i2c = FakeI2C()
    fake_socket = FakeSocket()
    hardware = HardwareDependencies(
        gpio=gpio,
        i2c_factory=lambda: i2c,
        expander_factory=FakeExpander,
        pull_up="PULL_UP",
        socket_factory=lambda: fake_socket,
    )
    shutdown_event = threading.Event()

    runtime = create_runtime(hardware, shutdown_event)

    assert gpio.calls[:3] == [
        ("setmode", gpio.BCM),
        ("setup", ENABLE_PIN, gpio.OUT, None),
        ("output", ENABLE_PIN, gpio.HIGH),
    ]

    runtime.start()
    runtime.close()
    runtime.close()

    assert fake_socket.bound == (SERVER_INCOMING_HOST, SERVER_INCOMING_PORT)
    assert fake_socket.closed
    assert i2c.deinitialized
    cleanup_index = gpio.calls.index(("cleanup",))
    assert ("output", ENABLE_PIN, gpio.HIGH) in gpio.calls[:cleanup_index]
    assert gpio.calls.count(("cleanup",)) == 1