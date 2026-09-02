from __future__ import annotations

import threading

from test_protocol import FakeController

from sample_stage_ctl.protocol import StepperNetworkComm


class RecordingSocket:
    def __init__(self) -> None:
        self.sent: list[tuple[bytes, tuple[str, int]]] = []
        self.closed = False

    def bind(self, _address: tuple[str, int]) -> None:
        pass

    def settimeout(self, _value: float) -> None:
        pass

    def recvfrom(self, _buffer_size: int) -> tuple[bytes, tuple[str, int]]:
        raise TimeoutError

    def sendto(self, data: bytes, address: tuple[str, int]) -> int:
        self.sent.append((data, address))
        return len(data)

    def close(self) -> None:
        self.closed = True


def test_connection_request_selects_status_destination() -> None:
    controller = FakeController()
    sock = RecordingSocket()
    network = StepperNetworkComm(
        controller,
        threading.Event(),
        socket_factory=lambda: sock,
    )

    network._handle_datagram(b"REQ_CONN", ("10.11.13.1", 11756))
    network._send_status_once()
    network.close()

    assert sock.sent == [
        (
            b"S,-1;E,True;P,0,1600;M,0,False;H,0,None;"
            b"P,1,-25;M,1,True;H,1,12;",
            ("10.11.13.1", 11756),
        )
    ]


def test_command_is_applied_and_acknowledged() -> None:
    controller = FakeController()
    sock = RecordingSocket()
    network = StepperNetworkComm(
        controller,
        threading.Event(),
        socket_factory=lambda: sock,
    )
    address = ("10.11.13.1", 11756)

    network._handle_datagram(b"REQ_CONN", address)
    network._handle_datagram(b"7,MOVE,0,1600", address)
    network._send_status_once()
    network.close()

    assert controller.calls == [("MOVE", 0, 1600)]
    assert sock.sent[0][0].startswith(b"S,7;")