from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

from sample_stage_ctl.workers import WorkerThread

ROTATION_STEP_PIN = 20
ROTATION_DIRECTION_PIN = 21
ROTATION_MAX_VELOCITY = 400
ROTATION_ACCELERATION = 1_000
LINEAR_STEP_PIN = 16
LINEAR_DIRECTION_PIN = 26
LINEAR_MAX_VELOCITY = 20_000
LINEAR_ACCELERATION = 20_000
ENABLE_PIN = 5
LIMIT_INTERRUPT_PIN = 7
LIMIT_EXPANDER_ADDRESS = 0x27
ROTATION_LIMIT_PIN = 1

ENABLE_DELAY_SECONDS = 2.0
IDLE_DISABLE_SECONDS = 10.0
CONTROLLER_INTERVAL_SECONDS = 0.1
LIMIT_POLL_SECONDS = 0.001
WORKER_JOIN_TIMEOUT_SECONDS = 2.0


class GPIOBackend(Protocol):
    OUT: object
    IN: object
    HIGH: object
    LOW: object
    PUD_UP: object

    def setup(
        self,
        channel: int,
        mode: object,
        pull_up_down: object | None = None,
    ) -> None: ...

    def output(self, channel: int, value: object) -> None: ...

    def input(self, channel: int) -> object: ...


class Stepper:
    def __init__(
        self,
        gpio: GPIOBackend,
        shutdown_event: threading.Event,
        step_pin: int,
        direction_pin: int,
        max_velocity: int,
        acceleration: int,
    ) -> None:
        self._gpio = gpio
        self._shutdown_event = shutdown_event
        self._step_pin = step_pin
        self._direction_pin = direction_pin
        self._max_velocity = max_velocity
        self._acceleration = acceleration
        self._homing_active = False
        self._home_triggered = False
        self._home_position: int | None = None
        self._homing_speed = max_velocity
        self._can_move = False
        self._target = 0
        self._position = 0
        self._motion_start_position = 0
        self._motion_direction = 0
        self._current_velocity = 0.0
        self._braking = False
        self._lock = threading.Lock()

        self._gpio.setup(self._direction_pin, self._gpio.OUT)
        self._gpio.setup(self._step_pin, self._gpio.OUT)
        self._worker = WorkerThread(
            f"stepper-{step_pin}",
            self._run,
            self._shutdown_event,
        )

    def start(self) -> None:
        self._worker.start()

    def close(self, timeout: float = WORKER_JOIN_TIMEOUT_SECONDS) -> None:
        self._shutdown_event.set()
        self._gpio.output(self._step_pin, self._gpio.LOW)
        self._worker.join(timeout)

    def raise_if_failed(self) -> None:
        self._worker.raise_if_failed()

    def set_target(self, target: int) -> None:
        with self._lock:
            was_idle = self._position == self._target
            new_direction = (target > self._position) - (target < self._position)
            if was_idle or new_direction != self._motion_direction:
                self._reset_profile_locked()
            self._target = target

    def get_position(self) -> int:
        with self._lock:
            return self._position

    def is_moving(self) -> bool:
        with self._lock:
            return self._position != self._target

    def can_move(self, value: bool) -> None:
        with self._lock:
            self._can_move = value
            if not value:
                self._reset_profile_locked()

    def set_position(self, position: int) -> None:
        with self._lock:
            self._position = position
            self._reset_profile_locked()

    def set_homing_state(self, to_home: bool, speed: int) -> None:
        with self._lock:
            self._homing_active = to_home
            self._homing_speed = speed
            self._home_position = None
            self._home_triggered = False
            self._braking = False

    def get_home_position(self) -> int | None:
        with self._lock:
            return self._home_position

    def trig_home(self) -> None:
        with self._lock:
            if not self._homing_active or self._home_triggered:
                return
            self._home_position = self._position
            self._home_triggered = True

            if self._motion_direction == 0 or self._current_velocity <= 0:
                self._position = 0
                self._target = 0
                self._homing_active = False
                self._reset_profile_locked()
                return

            braking_steps = max(
                1,
                math.ceil(
                    self._current_velocity**2 / (2.0 * self._acceleration)
                ),
            )
            direction = self._motion_direction
            self._position = -direction * braking_steps
            self._target = 0
            self._motion_start_position = self._position - direction * braking_steps
            self._braking = True

    def _reset_profile_locked(self) -> None:
        self._motion_start_position = self._position
        self._motion_direction = 0
        self._current_velocity = 0.0
        self._braking = False

    def _run(self) -> None:
        while not self._shutdown_event.is_set():
            delay = self._advance_once()
            if delay is None:
                self._shutdown_event.wait(0.01)
                continue
            if self._shutdown_event.wait(delay):
                self._gpio.output(self._step_pin, self._gpio.LOW)
                return
            self._gpio.output(self._step_pin, self._gpio.LOW)
            if self._shutdown_event.wait(delay):
                return

    def _advance_once(self) -> float | None:
        with self._lock:
            if not self._can_move or self._position == self._target:
                return None

            direction = 1 if self._position < self._target else -1
            if direction != self._motion_direction:
                self._reset_profile_locked()
                self._motion_direction = direction

            distance_from_start = abs(self._position - self._motion_start_position)
            distance_to_target = abs(self._target - self._position)
            acceleration_velocity = math.sqrt(
                2.0 * self._acceleration * (distance_from_start + 0.5)
            )
            deceleration_velocity = math.sqrt(
                2.0 * self._acceleration * (distance_to_target - 0.5)
            )
            velocity_limit = (
                self._homing_speed if self._homing_active else self._max_velocity
            )
            velocity = min(
                float(velocity_limit),
                acceleration_velocity,
                deceleration_velocity,
            )
            if self._braking:
                velocity = min(velocity, self._current_velocity)
            self._current_velocity = velocity

            if direction > 0:
                self._position += 1
                direction = self._gpio.HIGH
            else:
                self._position -= 1
                direction = self._gpio.LOW

            if self._position == self._target and self._home_triggered:
                self._homing_active = False
                self._home_triggered = False
                self._braking = False

        self._gpio.output(self._direction_pin, direction)
        self._gpio.output(self._step_pin, self._gpio.HIGH)
        return 1.0 / velocity / 2.0


class StepperController:
    def __init__(
        self,
        gpio: GPIOBackend,
        shutdown_event: threading.Event,
        enable_pin: int,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._gpio = gpio
        self._shutdown_event = shutdown_event
        self._enable_pin = enable_pin
        self._clock = clock
        self._steppers: list[Stepper] = []
        self._enabled = False
        self._last_move = 0.0
        self._worker = WorkerThread(
            "stepper-controller",
            self._run,
            self._shutdown_event,
        )

    def start(self) -> None:
        self._worker.start()

    def close(self, timeout: float = WORKER_JOIN_TIMEOUT_SECONDS) -> None:
        self._shutdown_event.set()
        self.disable_now()
        self._worker.join(timeout)

    def raise_if_failed(self) -> None:
        self._worker.raise_if_failed()

    def disable_now(self) -> None:
        for stepper in self._steppers:
            stepper.can_move(False)
        self._gpio.output(self._enable_pin, self._gpio.HIGH)
        self._enabled = False

    def set_stepper(self, stepper: int, target: int) -> None:
        self._steppers[stepper].set_target(target)

    def set_homing_state(self, stepper: int, state: bool, speed: int) -> None:
        self._steppers[stepper].set_homing_state(state, speed)

    def get_stepper(self, stepper: int) -> int:
        return self._steppers[stepper].get_position()

    def get_home_position(self, stepper: int) -> int | None:
        return self._steppers[stepper].get_home_position()

    def is_moving(self, stepper: int) -> bool:
        return self._steppers[stepper].is_moving()

    def is_enabled(self) -> bool:
        return self._enabled

    def set_stepper_position(self, stepper: int, position: int) -> None:
        self._steppers[stepper].set_position(position)

    def add_stepper(self, stepper: Stepper) -> None:
        self._steppers.append(stepper)

    def num_steppers(self) -> int:
        return len(self._steppers)

    def _run(self) -> None:
        while not self._shutdown_event.wait(CONTROLLER_INTERVAL_SECONDS):
            moving = any(stepper.is_moving() for stepper in self._steppers)
            if moving:
                self._last_move = self._clock()

            if moving and not self._enabled:
                self._gpio.output(self._enable_pin, self._gpio.LOW)
                if self._shutdown_event.wait(ENABLE_DELAY_SECONDS):
                    self.disable_now()
                    return
                for stepper in self._steppers:
                    stepper.can_move(True)
                self._enabled = True

            if (
                not moving
                and self._enabled
                and self._clock() - self._last_move > IDLE_DISABLE_SECONDS
            ):
                for stepper in self._steppers:
                    stepper.can_move(False)
                if self._shutdown_event.wait(ENABLE_DELAY_SECONDS):
                    self.disable_now()
                    return
                self._gpio.output(self._enable_pin, self._gpio.HIGH)
                self._enabled = False


class LimitSwitchController:
    def __init__(
        self,
        gpio: GPIOBackend,
        shutdown_event: threading.Event,
        i2c_factory: Callable[[], Any],
        expander_factory: Callable[..., Any],
        pull_up: object,
        address: int,
        interrupt_pin: int,
    ) -> None:
        self._gpio = gpio
        self._shutdown_event = shutdown_event
        self._i2c = i2c_factory()
        self._expander = expander_factory(self._i2c, address=address)
        self._pull_up = pull_up
        self._interrupt_pin = interrupt_pin
        self._steppers: list[tuple[Stepper, Any]] = []

        self._gpio.setup(
            self._interrupt_pin,
            self._gpio.IN,
            pull_up_down=self._gpio.PUD_UP,
        )
        self._worker = WorkerThread(
            "limit-switch-controller",
            self._run,
            self._shutdown_event,
        )

    def start(self) -> None:
        self._worker.start()

    def close(self, timeout: float = WORKER_JOIN_TIMEOUT_SECONDS) -> None:
        self._shutdown_event.set()
        self._worker.join(timeout)
        deinit = getattr(self._i2c, "deinit", None)
        if callable(deinit):
            deinit()

    def raise_if_failed(self) -> None:
        self._worker.raise_if_failed()

    def attach(self, stepper: Stepper, pin_number: int) -> None:
        pin = self._expander.get_pin(pin_number)
        pin.switch_to_input(pull=self._pull_up)
        self._steppers.append((stepper, pin))

    def _run(self) -> None:
        while not self._shutdown_event.is_set():
            if self._gpio.input(self._interrupt_pin) != self._gpio.LOW:
                self._shutdown_event.wait(LIMIT_POLL_SECONDS)
                continue

            for stepper, pin in self._steppers:
                while not self._shutdown_event.is_set():
                    try:
                        value = pin.value
                    except OSError:
                        self._shutdown_event.wait(LIMIT_POLL_SECONDS)
                        continue
                    if value:
                        stepper.trig_home()
                    break
