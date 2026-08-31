from __future__ import annotations

from collections import deque
import logging
import math
import threading
import time
from typing import Callable

import pyvisa
import serial

from ipi_ecs.core import daemon

from chamber_ctl.subsystems.laser_provider import LaserSyncProvider, LaserSyncProviderStatus


LOGGER = logging.getLogger(__name__)

CHOPPER_PORT = "COM4"
WAVEFORM_RESOURCE = "USB0::0x0957::0x1507::MY48009073::INSTR"
CHOPPER_RESPONSE_LINE_LIMIT = 8


class WFLaserSyncProvider(LaserSyncProvider):
    def __init__(
        self,
        laser_warmup_time: float = 5.0,
        chopper_startup_time: float = 5.0,
        *,
        chopper_port: str = CHOPPER_PORT,
        waveform_resource: str = WAVEFORM_RESOURCE,
        frequency_tolerance_hz: float = 2.0,
        recovery_max_attempts: int = 5,
        recovery_window_seconds: float = 120.0,
        recovery_delay_seconds: float = 5.0,
        reconnect_interval_seconds: float = 2.0,
        status_poll_interval_seconds: float = 1.0,
        serial_factory: Callable[..., object] = serial.Serial,
        visa_resource_manager_factory: Callable[[], object] = pyvisa.ResourceManager,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        if not math.isfinite(frequency_tolerance_hz) or frequency_tolerance_hz <= 0:
            raise ValueError("frequency_tolerance_hz must be finite and positive.")
        if recovery_max_attempts < 1:
            raise ValueError("recovery_max_attempts must be positive.")
        for name, value in (
            ("recovery_window_seconds", recovery_window_seconds),
            ("recovery_delay_seconds", recovery_delay_seconds),
            ("reconnect_interval_seconds", reconnect_interval_seconds),
            ("status_poll_interval_seconds", status_poll_interval_seconds),
        ):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive.")

        self.__laser_warmup_time = float(laser_warmup_time)
        self.__chopper_startup_time = float(chopper_startup_time)
        self.__target_phase = 0.0
        self.__current_phase = 0.0
        self.__initial_phase = 0.0
        self.__skew_rate = 1.0

        self.__desired_laser_on = False
        self.__laser_on = False
        self.__laser_started_at: float | None = None
        self.__desired_chopper_on = False
        self.__target_chopper_frequency_hz: int | None = 192
        self.__measured_chopper_frequency_hz: float | None = None
        self.__frequency_needs_apply = False

        self.__chopper_port_name = chopper_port
        self.__waveform_resource_name = waveform_resource
        self.__frequency_tolerance_hz = float(frequency_tolerance_hz)
        self.__recovery_max_attempts = recovery_max_attempts
        self.__recovery_window_seconds = float(recovery_window_seconds)
        self.__recovery_delay_seconds = float(recovery_delay_seconds)
        self.__reconnect_interval_seconds = float(reconnect_interval_seconds)
        self.__status_poll_interval_seconds = float(status_poll_interval_seconds)
        self.__serial_factory = serial_factory
        self.__visa_resource_manager_factory = visa_resource_manager_factory
        self.__monotonic = monotonic

        self.__lock = threading.RLock()
        self.__port = None
        self.__waveform = None
        self.__visa_resource_manager = None
        self.__last_chopper_connect_attempt = float("-inf")
        self.__last_waveform_connect_attempt = float("-inf")
        self.__last_chopper_poll = float("-inf")
        self.__last_waveform_probe = float("-inf")
        self.__next_chopper_recovery_at = float("-inf")
        self.__chopper_attempts: deque[float] = deque()
        self.__chopper_recovery_exhausted = False
        self.__chopper_error: str | None = None
        self.__waveform_error: str | None = None

        self.__daemon = daemon.Daemon(exception_handler=self.__handle_daemon_exception)
        self.__daemon.add(target=self.__thread)

    def refresh_hardware_status(self) -> LaserSyncProviderStatus:
        with self.__lock:
            now = self.__monotonic()
            self.__ensure_chopper_connection_locked(now)
            self.__ensure_waveform_connection_locked(now)
            if self.__port_is_open_locked():
                if self.__frequency_needs_apply:
                    self.__apply_target_frequency_locked()
                if now - self.__last_chopper_poll >= self.__status_poll_interval_seconds:
                    self.__poll_chopper_frequency_locked(now)
            if self.__laser_on and not self.__chopper_is_at_target_locked():
                self.__disable_laser_for_chopper_fault_locked()
            elif self.__chopper_is_at_target_locked():
                self.__clear_chopper_error_locked()
            self.__recover_chopper_if_needed_locked(now)
            return self.__status_locked()

    def get_hardware_status(self) -> LaserSyncProviderStatus:
        with self.__lock:
            return self.__status_locked()

    def set_target_phase(self, phase: float) -> tuple[bool, str]:
        if not self.__is_finite_number(phase):
            return False, "Target phase must be finite."
        with self.__lock:
            self.__target_phase = float(phase)
        return True, f"Target phase set to {float(phase):.3f}."

    def get_target_phase(self) -> float:
        with self.__lock:
            return self.__target_phase

    def set_current_phase(self, phase: float) -> tuple[bool, str]:
        if not self.__is_finite_number(phase):
            return False, "Current phase must be finite."
        with self.__lock:
            self.__current_phase = float(phase)
        return True, f"Current phase set to {float(phase):.3f}."

    def get_current_phase(self) -> float:
        with self.__lock:
            return self.__current_phase

    def set_initial_phase(self, phase: float) -> tuple[bool, str]:
        if not self.__is_finite_number(phase):
            return False, "Initial phase must be finite."
        with self.__lock:
            self.__initial_phase = float(phase)
        return True, f"Initial phase set to {float(phase):.3f}."

    def get_initial_phase(self) -> float:
        with self.__lock:
            return self.__initial_phase

    def set_chopper_on(self, on: bool) -> tuple[bool, str]:
        if not isinstance(on, bool):
            return False, "Chopper state must be boolean."
        with self.__lock:
            now = self.__monotonic()
            self.__desired_chopper_on = on
            if not on:
                self.__chopper_attempts.clear()
                self.__chopper_recovery_exhausted = False
                self.__next_chopper_recovery_at = float("-inf")
                if self.__send_chopper_command_locked("enable=0"):
                    return True, "Chopper disable requested."
                return True, "Chopper disable requested; controller is disconnected."

            if self.__target_chopper_frequency_hz is None:
                self.__desired_chopper_on = False
                return False, "Cannot enable chopper without an integer target frequency."
            self.__chopper_recovery_exhausted = False
            self.__measured_chopper_frequency_hz = None
            self.__last_chopper_poll = float("-inf")
            ok, message = self.__attempt_chopper_enable_locked(now)
            if ok:
                return True, message
            if not self.__chopper_recovery_exhausted:
                return True, f"Chopper enable is pending: {message}"
            return False, message

    def get_chopper_on(self) -> bool:
        with self.__lock:
            return self.__chopper_is_at_target_locked()

    def get_chopper_starting_up(self) -> bool:
        with self.__lock:
            return self.__desired_chopper_on and not self.__chopper_is_at_target_locked() and not self.__chopper_recovery_exhausted

    def set_chopper_frequency_hz(self, frequency_hz: float) -> tuple[bool, str]:
        if not self.__is_finite_number(frequency_hz) or float(frequency_hz) <= 0:
            return False, "Chopper frequency must be finite and positive."
        if not float(frequency_hz).is_integer():
            return False, "MC2000B chopper frequency must be an integer number of Hz."

        target_hz = int(frequency_hz)
        with self.__lock:
            self.__target_chopper_frequency_hz = target_hz
            self.__frequency_needs_apply = True
            self.__chopper_attempts.clear()
            self.__chopper_recovery_exhausted = False
            now = self.__monotonic()
            if self.__ensure_chopper_connection_locked(now) and self.__apply_target_frequency_locked():
                return True, f"Chopper target frequency set to {target_hz} Hz."
            return True, f"Chopper target frequency set to {target_hz} Hz; waiting for controller reconnection."

    def get_chopper_frequency_hz(self) -> float | None:
        with self.__lock:
            return self.__measured_chopper_frequency_hz

    def set_laser_on(self, on: bool) -> tuple[bool, str]:
        if not isinstance(on, bool):
            return False, "Laser state must be boolean."
        with self.__lock:
            if on and not self.__chopper_is_at_target_locked():
                return False, "Cannot enable laser while chopper is not at its target frequency."
            if not on:
                self.__desired_laser_on = False
                self.__laser_on = False
                self.__laser_started_at = None
                if self.__waveform is None:
                    return True, "Laser disable requested; waveform generator is disconnected."
            if not self.__ensure_waveform_connection_locked(self.__monotonic()):
                self.__desired_laser_on = False
                self.__laser_on = False
                self.__laser_started_at = None
                return False, "Cannot control laser because waveform generator is disconnected."
            try:
                self.__waveform.write("OUTPut " + ("ON" if on else "OFF"))
            except Exception as exc:
                self.__mark_waveform_disconnected_locked(f"Waveform generator command failed: {exc}")
                self.__desired_laser_on = False
                self.__laser_on = False
                self.__laser_started_at = None
                return False, "Waveform generator command failed."

            if on:
                self.__desired_laser_on = True
                self.__laser_on = True
                self.__laser_started_at = self.__monotonic()
                return True, "Laser enabled, warmup in progress."
            return True, "Laser disabled."

    def get_laser_on(self) -> bool:
        with self.__lock:
            return self.__laser_on

    def get_laser_warming_up(self) -> bool:
        with self.__lock:
            if not self.__laser_on or self.__laser_started_at is None:
                return False
            return self.__monotonic() - self.__laser_started_at < self.__laser_warmup_time

    def set_skew_rate(self, skew_rate: float) -> tuple[bool, str]:
        if not self.__is_finite_number(skew_rate) or float(skew_rate) < 0:
            return False, "Skew rate must be finite and non-negative."
        with self.__lock:
            self.__skew_rate = float(skew_rate)
        return True, f"Skew rate set to {float(skew_rate):.3f} deg/s."

    def get_skew_rate(self) -> float:
        with self.__lock:
            return self.__skew_rate

    def set_laser_warmup_time(self, warmup_time: float) -> tuple[bool, str]:
        if not self.__is_finite_number(warmup_time) or float(warmup_time) < 0:
            return False, "Laser warmup time must be finite and non-negative."
        with self.__lock:
            self.__laser_warmup_time = float(warmup_time)
        return True, f"Laser warmup time set to {float(warmup_time):.3f} s."

    def get_laser_warmup_time(self) -> float:
        with self.__lock:
            return self.__laser_warmup_time

    def set_chopper_startup_time(self, startup_time: float) -> tuple[bool, str]:
        if not self.__is_finite_number(startup_time) or float(startup_time) < 0:
            return False, "Chopper startup time must be finite and non-negative."
        with self.__lock:
            self.__chopper_startup_time = float(startup_time)
        return True, f"Chopper startup time set to {float(startup_time):.3f} s."

    def get_chopper_startup_time(self) -> float:
        with self.__lock:
            return self.__chopper_startup_time

    def do_single_shot(self, shut_phase: float, open_phase: float, expose_time: float) -> tuple[bool, str]:
        if not self.__is_finite_number(expose_time) or float(expose_time) < 0:
            return False, "Exposure time must be finite and non-negative."
        if not self.get_chopper_on():
            return False, "Cannot do single shot while chopper is not at its target frequency."
        if self.get_laser_warming_up():
            return False, "Cannot do single shot while laser is warming up."
        ok, status = self.set_target_phase(open_phase)
        if not ok:
            return ok, status
        while abs(self.get_current_phase() - float(open_phase)) > 1e-2:
            time.sleep(0.01)
        time.sleep(float(expose_time))
        ok, status = self.set_target_phase(shut_phase)
        if not ok:
            return ok, status
        while abs(self.get_current_phase() - float(shut_phase)) > 1e-2:
            time.sleep(0.01)
        return True, f"Single shot completed with expose time {float(expose_time):.3f} s."

    def start(self) -> None:
        self.__daemon.start()

    def stop(self) -> None:
        self.__daemon.stop()
        with self.__lock:
            self.__mark_chopper_disconnected_locked("Chopper provider stopped.", log_level=logging.INFO)
            self.__mark_waveform_disconnected_locked("Waveform provider stopped.", log_level=logging.INFO)

    def __thread(self, stop_flag: daemon.StopFlag) -> None:
        last_time = self.__monotonic()
        while stop_flag.run():
            time.sleep(0.1)
            try:
                status = self.refresh_hardware_status()
                now = self.__monotonic()
                with self.__lock:
                    if status.laser_on and not status.chopper_starting_up:
                        self.__set_waveform_phase_locked(self.__current_phase)
                    dt = now - last_time
                    last_time = now
                    if status.laser_on and not status.chopper_starting_up:
                        self.__advance_phase_locked(dt)
            except Exception as exc:
                LOGGER.exception("Unexpected physical laser provider worker error: %s", exc)

    def __handle_daemon_exception(self, exc: Exception) -> None:
        LOGGER.error("Physical laser provider daemon failed: %s", exc)

    def __advance_phase_locked(self, dt: float) -> None:
        if self.__current_phase < self.__target_phase:
            self.__current_phase = min(self.__target_phase, self.__current_phase + self.__skew_rate * dt)
        elif self.__current_phase > self.__target_phase:
            self.__current_phase = max(self.__target_phase, self.__current_phase - self.__skew_rate * dt)

    def __ensure_chopper_connection_locked(self, now: float) -> bool:
        if self.__port_is_open_locked():
            return True
        if now - self.__last_chopper_connect_attempt < self.__reconnect_interval_seconds:
            return False
        self.__last_chopper_connect_attempt = now
        try:
            self.__port = self.__serial_factory(
                self.__chopper_port_name,
                baudrate=115200,
                bytesize=8,
                parity="N",
                stopbits=1,
                timeout=1.0,
                write_timeout=1.0,
            )
        except Exception as exc:
            self.__port = None
            self.__set_chopper_error_locked(f"Chopper controller unavailable on {self.__chopper_port_name}: {exc}")
            return False
        self.__frequency_needs_apply = self.__target_chopper_frequency_hz is not None
        self.__clear_chopper_error_locked()
        LOGGER.info("Connected to chopper controller on %s.", self.__chopper_port_name)
        return True

    def __ensure_waveform_connection_locked(self, now: float) -> bool:
        if self.__waveform is not None:
            if now - self.__last_waveform_probe >= self.__status_poll_interval_seconds:
                self.__last_waveform_probe = now
                query = getattr(self.__waveform, "query", None)
                if callable(query):
                    try:
                        query("*IDN?")
                    except Exception as exc:
                        self.__mark_waveform_disconnected_locked(f"Waveform generator probe failed: {exc}")
                        return False
            return True
        if now - self.__last_waveform_connect_attempt < self.__reconnect_interval_seconds:
            return False
        self.__last_waveform_connect_attempt = now
        try:
            if self.__visa_resource_manager is None:
                self.__visa_resource_manager = self.__visa_resource_manager_factory()
            waveform = self.__visa_resource_manager.open_resource(self.__waveform_resource_name)
            waveform.timeout = 1000
            waveform.write_termination = "\n"
            waveform.read_termination = "\n"
            self.__waveform = waveform
        except Exception as exc:
            self.__waveform = None
            self.__set_waveform_error_locked(f"Waveform generator unavailable at {self.__waveform_resource_name}: {exc}")
            return False
        self.__clear_waveform_error_locked()
        LOGGER.info("Connected to waveform generator at %s.", self.__waveform_resource_name)
        return True

    def __apply_target_frequency_locked(self) -> bool:
        if self.__target_chopper_frequency_hz is None:
            return False
        if not self.__send_chopper_command_locked(f"freq={self.__target_chopper_frequency_hz}"):
            return False
        self.__frequency_needs_apply = False
        return True

    def __poll_chopper_frequency_locked(self, now: float) -> None:
        self.__last_chopper_poll = now
        response = self.__query_chopper_command_locked("refoutfreq?")
        if response is None:
            return
        try:
            frequency_hz = float(response)
        except Exception as exc:
            self.__mark_chopper_disconnected_locked(f"Invalid chopper frequency response: {exc}")
            return
        if not math.isfinite(frequency_hz) or frequency_hz < 0:
            self.__set_chopper_error_locked(f"Invalid chopper frequency response: {response!r}")
            self.__measured_chopper_frequency_hz = None
            return
        self.__measured_chopper_frequency_hz = frequency_hz

    def __recover_chopper_if_needed_locked(self, now: float) -> None:
        if not self.__desired_chopper_on or self.__chopper_is_at_target_locked() or self.__chopper_recovery_exhausted:
            return
        if now < self.__next_chopper_recovery_at:
            return
        self.__attempt_chopper_enable_locked(now)

    def __attempt_chopper_enable_locked(self, now: float) -> tuple[bool, str]:
        self.__discard_expired_attempts_locked(now)
        if len(self.__chopper_attempts) >= self.__recovery_max_attempts:
            self.__chopper_recovery_exhausted = True
            self.__set_chopper_error_locked(
                f"Automatic chopper recovery exhausted after {self.__recovery_max_attempts} attempts in "
                f"{self.__recovery_window_seconds:.0f} seconds."
            )
            return False, self.__chopper_error
        self.__chopper_attempts.append(now)
        self.__next_chopper_recovery_at = now + self.__recovery_delay_seconds
        if not self.__ensure_chopper_connection_locked(now):
            return False, "Chopper controller is disconnected; retry remains pending."
        if not self.__apply_target_frequency_locked():
            return False, "Failed to apply chopper target frequency; retry remains pending."
        if not self.__send_chopper_command_locked("enable=1"):
            return False, "Failed to enable chopper; retry remains pending."
        attempt_number = len(self.__chopper_attempts)
        LOGGER.info(
            "Requested chopper enable attempt %d of %d at %d Hz.",
            attempt_number,
            self.__recovery_max_attempts,
            self.__target_chopper_frequency_hz,
        )
        return True, (
            f"Chopper enable requested at {self.__target_chopper_frequency_hz} Hz "
            f"(attempt {attempt_number} of {self.__recovery_max_attempts})."
        )

    def __discard_expired_attempts_locked(self, now: float) -> None:
        threshold = now - self.__recovery_window_seconds
        while self.__chopper_attempts and self.__chopper_attempts[0] < threshold:
            self.__chopper_attempts.popleft()

    def __send_chopper_command_locked(self, command: str) -> bool:
        now = self.__monotonic()
        if not self.__ensure_chopper_connection_locked(now):
            return False
        try:
            self.__write_chopper_command_locked(command)
            self.__read_chopper_response_locked(command, expect_value=False)
            return True
        except Exception as exc:
            self.__mark_chopper_disconnected_locked(f"Chopper command {command!r} failed: {exc}")
            return False

    def __query_chopper_command_locked(self, command: str) -> str | None:
        now = self.__monotonic()
        if not self.__ensure_chopper_connection_locked(now):
            return None
        try:
            self.__write_chopper_command_locked(command)
            return self.__read_chopper_response_locked(command, expect_value=True)
        except Exception as exc:
            self.__mark_chopper_disconnected_locked(f"Chopper command {command!r} failed: {exc}")
            return None

    def __write_chopper_command_locked(self, command: str) -> None:
        reset_input_buffer = getattr(self.__port, "reset_input_buffer", None)
        if callable(reset_input_buffer):
            reset_input_buffer()
        self.__port.write(f"{command}\r".encode("utf-8"))
        flush = getattr(self.__port, "flush", None)
        if callable(flush):
            flush()

    def __read_chopper_response_locked(self, command: str, *, expect_value: bool) -> str:
        for _ in range(CHOPPER_RESPONSE_LINE_LIMIT):
            response = self.__normalize_chopper_response(self.__read_chopper_line_locked())
            if not response:
                continue
            if response.casefold() == command.casefold():
                if expect_value:
                    continue
                return response
            return response
        raise TimeoutError("controller response contained no value")

    def __read_chopper_line_locked(self) -> str:
        response = self.__port.read_until(b"\r")
        if not response:
            raise TimeoutError("controller did not respond")
        return response.decode("utf-8").strip()

    @staticmethod
    def __normalize_chopper_response(response: str) -> str:
        normalized = response.strip()
        while normalized.startswith(">"):
            normalized = normalized[1:].lstrip()
        return normalized

    def __set_waveform_phase_locked(self, phase: float) -> bool:
        if self.__waveform is None:
            return False
        try:
            self.__waveform.write(f"BURSt:PHASe {phase}")
            return True
        except Exception as exc:
            self.__mark_waveform_disconnected_locked(f"Waveform generator phase command failed: {exc}")
            self.__laser_on = False
            self.__desired_laser_on = False
            self.__laser_started_at = None
            return False

    def __disable_laser_for_chopper_fault_locked(self) -> None:
        message = "Chopper left its target frequency while laser was enabled; laser disabled."
        if self.__waveform is not None:
            try:
                self.__waveform.write("OUTPut OFF")
            except Exception as exc:
                self.__mark_waveform_disconnected_locked(f"Waveform generator safety shutdown failed: {exc}")
        self.__desired_laser_on = False
        self.__laser_on = False
        self.__laser_started_at = None
        self.__set_chopper_error_locked(message, log_level=logging.ERROR)

    def __chopper_is_at_target_locked(self) -> bool:
        return (
            self.__desired_chopper_on
            and self.__port_is_open_locked()
            and self.__target_chopper_frequency_hz is not None
            and self.__measured_chopper_frequency_hz is not None
            and abs(self.__measured_chopper_frequency_hz - self.__target_chopper_frequency_hz)
            <= self.__frequency_tolerance_hz
        )

    def __status_locked(self) -> LaserSyncProviderStatus:
        measured_frequency_hz = self.__measured_chopper_frequency_hz
        chopper_spinning = measured_frequency_hz is not None and measured_frequency_hz > 0
        chopper_on = self.__chopper_is_at_target_locked()
        return LaserSyncProviderStatus(
            desired_laser_on=self.__desired_laser_on,
            laser_on=self.__laser_on,
            desired_chopper_on=self.__desired_chopper_on,
            chopper_on=chopper_on,
            chopper_starting_up=self.__desired_chopper_on and not chopper_on and not self.__chopper_recovery_exhausted,
            chopper_spinning=chopper_spinning,
            target_chopper_frequency_hz=self.__target_chopper_frequency_hz,
            measured_chopper_frequency_hz=measured_frequency_hz,
            chopper_connected=self.__port_is_open_locked(),
            waveform_connected=self.__waveform is not None,
            chopper_recovery_exhausted=self.__chopper_recovery_exhausted,
            chopper_error=self.__chopper_error,
            waveform_error=self.__waveform_error,
        )

    def __port_is_open_locked(self) -> bool:
        return self.__port is not None and bool(getattr(self.__port, "is_open", True))

    def __mark_chopper_disconnected_locked(self, message: str, *, log_level: int = logging.WARNING) -> None:
        port = self.__port
        self.__port = None
        self.__measured_chopper_frequency_hz = None
        if port is not None:
            try:
                port.close()
            except Exception:
                pass
        self.__set_chopper_error_locked(message, log_level=log_level)

    def __mark_waveform_disconnected_locked(self, message: str, *, log_level: int = logging.WARNING) -> None:
        waveform = self.__waveform
        self.__waveform = None
        self.__laser_on = False
        self.__desired_laser_on = False
        self.__laser_started_at = None
        if waveform is not None:
            try:
                waveform.close()
            except Exception:
                pass
        self.__set_waveform_error_locked(message, log_level=log_level)

    def __set_chopper_error_locked(self, message: str, *, log_level: int = logging.WARNING) -> None:
        if self.__chopper_error != message:
            LOGGER.log(log_level, "%s", message)
        self.__chopper_error = message

    def __clear_chopper_error_locked(self) -> None:
        if self.__chopper_error is not None and not self.__chopper_recovery_exhausted:
            LOGGER.info("Chopper controller communication recovered.")
            self.__chopper_error = None

    def __set_waveform_error_locked(self, message: str, *, log_level: int = logging.WARNING) -> None:
        if self.__waveform_error != message:
            LOGGER.log(log_level, "%s", message)
        self.__waveform_error = message

    def __clear_waveform_error_locked(self) -> None:
        if self.__waveform_error is not None:
            LOGGER.info("Waveform generator communication recovered.")
            self.__waveform_error = None

    @staticmethod
    def __is_finite_number(value: object) -> bool:
        return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(float(value))