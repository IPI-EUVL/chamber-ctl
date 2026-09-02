from __future__ import annotations

import socket
import threading
from dataclasses import dataclass
from collections.abc import Callable
from typing import Protocol

from sample_stage_ctl.workers import WorkerThread

SERVER_INCOMING_HOST = "0.0.0.0"
SERVER_INCOMING_PORT = 11755
STATUS_INTERVAL_SECONDS = 0.05
RECEIVE_TIMEOUT_SECONDS = 0.1
RECEIVE_BUFFER_BYTES = 1024
WORKER_JOIN_TIMEOUT_SECONDS = 2.0

SocketAddress = tuple[str, int]


class StageController(Protocol):
    def set_stepper(self, stepper: int, target: int) -> None: ...

    def set_stepper_position(self, stepper: int, position: int) -> None: ...

    def set_homing_state(self, stepper: int, state: bool, speed: int) -> None: ...

    def get_stepper(self, stepper: int) -> int: ...

    def get_home_position(self, stepper: int) -> int | None: ...

    def is_moving(self, stepper: int) -> bool: ...

    def is_enabled(self) -> bool: ...

    def num_steppers(self) -> int: ...


class DatagramSocket(Protocol):
    def bind(self, address: SocketAddress) -> None: ...

    def settimeout(self, value: float) -> None: ...

    def recvfrom(self, buffer_size: int) -> tuple[bytes, SocketAddress]: ...

    def sendto(self, data: bytes, address: SocketAddress) -> int: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class ConnectionRequest:
    pass


@dataclass(frozen=True)
class StageCommand:
    sequence: str
    name: str
    arguments: tuple[int | bool, ...]


def parse_datagram(data: bytes) -> ConnectionRequest | StageCommand:
    fields = data.decode("utf-8").strip().split(",")
    if fields[0] == "REQ_CONN":
        return ConnectionRequest()

    sequence = fields[0]
    command = fields[1]
    if command == "MOVE":
        arguments: tuple[int | bool, ...] = (int(fields[2]), int(fields[3]))
    elif command == "SET":
        arguments = (int(fields[2]), int(fields[3]))
    elif command == "HOME":
        arguments = (int(fields[2]), fields[3] == "T", int(fields[4]))
    else:
        arguments = ()

    return StageCommand(sequence, command, arguments)


def apply_command(controller: StageController, command: StageCommand) -> None:
    if command.name == "MOVE":
        stepper, target = command.arguments
        controller.set_stepper(int(stepper), int(target))
    elif command.name == "SET":
        stepper, position = command.arguments
        controller.set_stepper_position(int(stepper), int(position))
    elif command.name == "HOME":
        stepper, state, speed = command.arguments
        controller.set_homing_state(int(stepper), bool(state), int(speed))


def format_status(controller: StageController, sequence: int | str) -> bytes:
    fields = [f"S,{sequence}", f"E,{controller.is_enabled()}"]
    for stepper in range(controller.num_steppers()):
        fields.append(f"P,{stepper},{controller.get_stepper(stepper)}")
        fields.append(f"M,{stepper},{controller.is_moving(stepper)}")
        fields.append(f"H,{stepper},{controller.get_home_position(stepper)}")
    return (";".join(fields) + ";").encode("utf-8")


def _default_socket_factory() -> DatagramSocket:
    return socket.socket(socket.AF_INET, socket.SOCK_DGRAM)


class StepperNetworkComm:
    def __init__(
        self,
        controller: StageController,
        shutdown_event: threading.Event,
        *,
        host: str = SERVER_INCOMING_HOST,
        port: int = SERVER_INCOMING_PORT,
        socket_factory: Callable[[], DatagramSocket] = _default_socket_factory,
    ) -> None:
        self._controller = controller
        self._shutdown_event = shutdown_event
        self._host = host
        self._port = port
        self._socket = socket_factory()
        self._remote_address: SocketAddress | None = None
        self._current_sequence: int | str = -1
        self._state_lock = threading.Lock()
        self._closed = False
        self._sender = WorkerThread(
            "sample-stage-status-sender",
            self._send_loop,
            self._shutdown_event,
        )
        self._receiver = WorkerThread(
            "sample-stage-command-receiver",
            self._receive_loop,
            self._shutdown_event,
        )

    def start(self) -> None:
        self._socket.bind((self._host, self._port))
        self._socket.settimeout(RECEIVE_TIMEOUT_SECONDS)
        self._receiver.start()
        self._sender.start()

    def request_stop(self) -> None:
        self._shutdown_event.set()
        if self._closed:
            return
        self._closed = True
        try:
            self._socket.close()
        except OSError:
            pass

    def close(self, timeout: float = WORKER_JOIN_TIMEOUT_SECONDS) -> None:
        self.request_stop()
        self._receiver.join(timeout)
        self._sender.join(timeout)

    def raise_if_failed(self) -> None:
        self._receiver.raise_if_failed()
        self._sender.raise_if_failed()

    def _handle_datagram(self, data: bytes, address: SocketAddress) -> None:
        request = parse_datagram(data)
        with self._state_lock:
            if isinstance(request, ConnectionRequest):
                self._remote_address = address
                return
            self._current_sequence = request.sequence
        apply_command(self._controller, request)

    def _send_status_once(self) -> None:
        with self._state_lock:
            address = self._remote_address
            sequence = self._current_sequence
        if address is not None:
            self._socket.sendto(format_status(self._controller, sequence), address)

    def _send_loop(self) -> None:
        while not self._shutdown_event.is_set():
            try:
                self._send_status_once()
            except OSError:
                if self._shutdown_event.is_set():
                    return
                raise
            self._shutdown_event.wait(STATUS_INTERVAL_SECONDS)

    def _receive_loop(self) -> None:
        while not self._shutdown_event.is_set():
            try:
                data, address = self._socket.recvfrom(RECEIVE_BUFFER_BYTES)
            except TimeoutError:
                continue
            except ConnectionResetError:
                continue
            except OSError:
                if self._shutdown_event.is_set():
                    return
                raise
            self._handle_datagram(data, address)
