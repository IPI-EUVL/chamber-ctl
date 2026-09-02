from __future__ import annotations

import threading
from dataclasses import dataclass, field

import pytest

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


@dataclass
class FakeGPIO:
    OUT: str = "OUT"
    IN: str = "IN"
    HIGH: int = 1
    LOW: int = 0
    PUD_UP: str = "PUD_UP"
    calls: list[tuple[object, ...]] = field(default_factory=list)
    inputs: dict[int, int] = field(default_factory=dict)

    def setup(
        self,
        channel: int,
        mode: object,
        pull_up_down: object | None = None,
    ) -> None:
        self.calls.append(("setup", channel, mode, pull_up_down))

    def output(self, channel: int, value: object) -> None:
        self.calls.append(("output", channel, value))

    def input(self, channel: int) -> object:
        return self.inputs.get(channel, self.HIGH)


class FakePin:
    def __init__(self) -> None:
        self.value = False
        self.pull: object | None = None

    def switch_to_input(self, *, pull: object) -> None:
        self.pull = pull


class FakeExpander:
    def __init__(self, _i2c: object, *, address: int) -> None:
        self.address = address
        self.pins: dict[int, FakePin] = {}

    def get_pin(self, pin_number: int) -> FakePin:
        return self.pins.setdefault(pin_number, FakePin())


def make_stepper(gpio: FakeGPIO, shutdown_event: threading.Event) -> Stepper:
    return Stepper(
        gpio,
        shutdown_event,
        ROTATION_STEP_PIN,
        ROTATION_DIRECTION_PIN,
        ROTATION_MAX_VELOCITY,
        ROTATION_ACCELERATION,
    )


def test_hardware_constants_match_legacy_controller() -> None:
    assert (ROTATION_STEP_PIN, ROTATION_DIRECTION_PIN, ROTATION_MAX_VELOCITY) == (
        20,
        21,
        400,
    )
    assert (LINEAR_STEP_PIN, LINEAR_DIRECTION_PIN, LINEAR_MAX_VELOCITY) == (
        16,
        26,
        20_000,
    )
    assert (ROTATION_ACCELERATION, LINEAR_ACCELERATION) == (1_000, 20_000)
    assert (ENABLE_PIN, LIMIT_INTERRUPT_PIN, LIMIT_EXPANDER_ADDRESS) == (5, 7, 0x27)
    assert ROTATION_LIMIT_PIN == 1


def test_stepper_construction_configures_pins_without_starting_motion() -> None:
    gpio = FakeGPIO()
    stepper = make_stepper(gpio, threading.Event())

    assert gpio.calls == [
        ("setup", ROTATION_DIRECTION_PIN, gpio.OUT, None),
        ("setup", ROTATION_STEP_PIN, gpio.OUT, None),
    ]
    assert stepper.get_position() == 0
    assert not stepper.is_moving()


def test_stepper_advances_in_both_directions() -> None:
    gpio = FakeGPIO()
    stepper = make_stepper(gpio, threading.Event())
    stepper.can_move(True)

    stepper.set_target(1)
    assert stepper._advance_once() is not None
    assert stepper.get_position() == 1
    assert gpio.calls[-2:] == [
        ("output", ROTATION_DIRECTION_PIN, gpio.HIGH),
        ("output", ROTATION_STEP_PIN, gpio.HIGH),
    ]

    stepper.set_target(0)
    assert stepper._advance_once() is not None
    assert stepper.get_position() == 0
    assert gpio.calls[-2:] == [
        ("output", ROTATION_DIRECTION_PIN, gpio.LOW),
        ("output", ROTATION_STEP_PIN, gpio.HIGH),
    ]


def test_long_move_uses_symmetric_trapezoidal_profile() -> None:
    gpio = FakeGPIO()
    stepper = make_stepper(gpio, threading.Event())
    stepper.can_move(True)
    stepper.set_target(200)

    delays = [stepper._advance_once() for _ in range(200)]
    velocities = [1.0 / (2.0 * delay) for delay in delays if delay is not None]

    assert len(velocities) == 200
    assert velocities[0] == pytest.approx(ROTATION_ACCELERATION**0.5)
    assert velocities[1] ** 2 - velocities[0] ** 2 == pytest.approx(
        2 * ROTATION_ACCELERATION
    )
    assert max(velocities) == pytest.approx(ROTATION_MAX_VELOCITY)
    assert velocities.count(ROTATION_MAX_VELOCITY) == 40
    assert velocities[:80] == sorted(velocities[:80])
    assert velocities[-80:] == sorted(velocities[-80:], reverse=True)
    assert velocities == pytest.approx(list(reversed(velocities)))


def test_short_move_uses_symmetric_triangular_profile() -> None:
    gpio = FakeGPIO()
    stepper = make_stepper(gpio, threading.Event())
    stepper.can_move(True)
    stepper.set_target(20)

    delays = [stepper._advance_once() for _ in range(20)]
    velocities = [1.0 / (2.0 * delay) for delay in delays if delay is not None]

    assert max(velocities) < ROTATION_MAX_VELOCITY
    assert velocities == pytest.approx(list(reversed(velocities)))


def test_homing_accelerates_to_its_requested_speed() -> None:
    gpio = FakeGPIO()
    stepper = make_stepper(gpio, threading.Event())
    stepper.can_move(True)
    stepper.set_homing_state(True, 50)
    stepper.set_target(10)

    delays = [stepper._advance_once() for _ in range(5)]
    velocities = [1.0 / (2.0 * delay) for delay in delays if delay is not None]

    assert velocities[0] < 50
    assert max(velocities) == pytest.approx(50)


def test_homing_trigger_decelerates_past_sensor_and_finishes_at_zero() -> None:
    gpio = FakeGPIO()
    stepper = make_stepper(gpio, threading.Event())
    stepper.can_move(True)
    stepper.set_homing_state(True, ROTATION_MAX_VELOCITY)
    stepper.set_target(1_000)

    for _ in range(100):
        stepper._advance_once()

    stepper.trig_home()

    assert stepper.get_home_position() == 100
    assert stepper.is_moving()

    coast_delays = []
    while stepper.is_moving():
        coast_delays.append(stepper._advance_once())

    coast_velocities = [
        1.0 / (2.0 * delay) for delay in coast_delays if delay is not None
    ]
    assert len(coast_velocities) == 80
    assert coast_velocities == sorted(coast_velocities, reverse=True)
    assert stepper.get_position() == 0


def test_homing_trigger_records_position_and_resets_coordinates() -> None:
    gpio = FakeGPIO()
    stepper = make_stepper(gpio, threading.Event())
    stepper.set_position(123)
    stepper.set_target(456)
    stepper.set_homing_state(True, 100)

    stepper.trig_home()

    assert stepper.get_home_position() == 123
    assert stepper.get_position() == 0
    assert not stepper.is_moving()


def test_disable_now_stops_motion_before_driving_enable_high() -> None:
    gpio = FakeGPIO()
    shutdown_event = threading.Event()
    stepper = make_stepper(gpio, shutdown_event)
    controller = StepperController(gpio, shutdown_event, ENABLE_PIN)
    controller.add_stepper(stepper)
    stepper.can_move(True)
    stepper.set_target(10)

    controller.disable_now()

    assert stepper._advance_once() is None
    assert gpio.calls[-1] == ("output", ENABLE_PIN, gpio.HIGH)
    assert not controller.is_enabled()


def test_limit_switch_uses_legacy_expander_configuration() -> None:
    gpio = FakeGPIO()
    shutdown_event = threading.Event()
    i2c = object()
    expander: FakeExpander | None = None

    def expander_factory(value: object, *, address: int) -> FakeExpander:
        nonlocal expander
        expander = FakeExpander(value, address=address)
        return expander

    limit = LimitSwitchController(
        gpio,
        shutdown_event,
        lambda: i2c,
        expander_factory,
        "PULL_UP",
        LIMIT_EXPANDER_ADDRESS,
        LIMIT_INTERRUPT_PIN,
    )
    stepper = make_stepper(gpio, shutdown_event)
    limit.attach(stepper, ROTATION_LIMIT_PIN)

    assert expander is not None
    assert expander.address == 0x27
    assert expander.pins[1].pull == "PULL_UP"
    assert ("setup", LIMIT_INTERRUPT_PIN, gpio.IN, gpio.PUD_UP) in gpio.calls