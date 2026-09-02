from __future__ import annotations

import importlib
import signal
import threading
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from sample_stage_ctl.controller import (
    ENABLE_PIN,
    LINEAR_ACCELERATION,
    LINEAR_DIRECTION_PIN,
    LINEAR_MAX_VELOCITY,
    LINEAR_STEP_PIN,
    LIMIT_EXPANDER_ADDRESS,
    LIMIT_INTERRUPT_PIN,
    ROTATION_DIRECTION_PIN,
    ROTATION_LIMIT_PIN,
    ROTATION_ACCELERATION,
    ROTATION_MAX_VELOCITY,
    ROTATION_STEP_PIN,
    LimitSwitchController,
    Stepper,
    StepperController,
)
from sample_stage_ctl.protocol import StepperNetworkComm

HEALTH_CHECK_INTERVAL_SECONDS = 0.1


@dataclass(frozen=True)
class HardwareDependencies:
    gpio: Any
    i2c_factory: Any
    expander_factory: Any
    pull_up: object
    socket_factory: Any | None = None


def load_hardware_dependencies() -> HardwareDependencies:
    gpio = importlib.import_module("RPi.GPIO")
    board = importlib.import_module("board")
    pcf8574 = importlib.import_module("adafruit_pcf8574")
    digitalio = importlib.import_module("digitalio")
    return HardwareDependencies(
        gpio=gpio,
        i2c_factory=board.I2C,
        expander_factory=pcf8574.PCF8574,
        pull_up=digitalio.Pull.UP,
    )


class StageRuntime:
    def __init__(
        self,
        hardware: HardwareDependencies,
        shutdown_event: threading.Event,
        rotation: Stepper,
        linear: Stepper,
        controller: StepperController,
        limits: LimitSwitchController,
        network: StepperNetworkComm,
    ) -> None:
        self._hardware = hardware
        self._shutdown_event = shutdown_event
        self._rotation = rotation
        self._linear = linear
        self._controller = controller
        self._limits = limits
        self._network = network
        self._closed = False

    def start(self) -> None:
        self._rotation.start()
        self._linear.start()
        self._controller.start()
        self._limits.start()
        self._network.start()

    def run(self) -> None:
        while not self._shutdown_event.wait(HEALTH_CHECK_INTERVAL_SECONDS):
            self.raise_if_failed()
        self.raise_if_failed()

    def raise_if_failed(self) -> None:
        self._rotation.raise_if_failed()
        self._linear.raise_if_failed()
        self._controller.raise_if_failed()
        self._limits.raise_if_failed()
        self._network.raise_if_failed()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._shutdown_event.set()
        errors: list[BaseException] = []

        try:
            self._network.request_stop()
        except BaseException as exc:
            errors.append(exc)
        try:
            self._controller.disable_now()
        except BaseException as exc:
            errors.append(exc)

        for component in (
            self._network,
            self._limits,
            self._controller,
            self._rotation,
            self._linear,
        ):
            try:
                component.close()
            except BaseException as exc:
                errors.append(exc)

        try:
            self._hardware.gpio.cleanup()
        except BaseException as exc:
            errors.append(exc)

        if errors:
            raise ExceptionGroup("Sample-stage shutdown failed", errors)


def create_runtime(
    hardware: HardwareDependencies,
    shutdown_event: threading.Event | None = None,
) -> StageRuntime:
    stop = shutdown_event or threading.Event()
    gpio = hardware.gpio
    gpio.setmode(gpio.BCM)
    gpio.setup(ENABLE_PIN, gpio.OUT)
    gpio.output(ENABLE_PIN, gpio.HIGH)

    rotation: Stepper | None = None
    linear: Stepper | None = None
    controller: StepperController | None = None
    limits: LimitSwitchController | None = None
    network: StepperNetworkComm | None = None
    try:
        rotation = Stepper(
            gpio,
            stop,
            ROTATION_STEP_PIN,
            ROTATION_DIRECTION_PIN,
            ROTATION_MAX_VELOCITY,
            ROTATION_ACCELERATION,
        )
        linear = Stepper(
            gpio,
            stop,
            LINEAR_STEP_PIN,
            LINEAR_DIRECTION_PIN,
            LINEAR_MAX_VELOCITY,
            LINEAR_ACCELERATION,
        )
        controller = StepperController(gpio, stop, ENABLE_PIN)
        controller.add_stepper(rotation)
        controller.add_stepper(linear)
        limits = LimitSwitchController(
            gpio,
            stop,
            hardware.i2c_factory,
            hardware.expander_factory,
            hardware.pull_up,
            LIMIT_EXPANDER_ADDRESS,
            LIMIT_INTERRUPT_PIN,
        )
        limits.attach(rotation, ROTATION_LIMIT_PIN)
        network_arguments: dict[str, Any] = {}
        if hardware.socket_factory is not None:
            network_arguments["socket_factory"] = hardware.socket_factory
        network = StepperNetworkComm(controller, stop, **network_arguments)
        return StageRuntime(
            hardware,
            stop,
            rotation,
            linear,
            controller,
            limits,
            network,
        )
    except BaseException:
        stop.set()
        if network is not None:
            with suppress(BaseException):
                network.close()
        if controller is not None:
            with suppress(BaseException):
                controller.disable_now()
            with suppress(BaseException):
                controller.close()
        if limits is not None:
            with suppress(BaseException):
                limits.close()
        for stepper in (rotation, linear):
            if stepper is not None:
                with suppress(BaseException):
                    stepper.close()
        with suppress(BaseException):
            gpio.output(ENABLE_PIN, gpio.HIGH)
        with suppress(BaseException):
            gpio.cleanup()
        raise


def install_signal_handlers(shutdown_event: threading.Event) -> None:
    def request_shutdown(_signum: int, _frame: object) -> None:
        shutdown_event.set()

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)


def main() -> None:
    shutdown_event = threading.Event()
    install_signal_handlers(shutdown_event)
    runtime = create_runtime(load_hardware_dependencies(), shutdown_event)
    try:
        runtime.start()
        runtime.run()
    finally:
        runtime.close()
