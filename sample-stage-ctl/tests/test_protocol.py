from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from sample_stage_ctl.protocol import (
    ConnectionRequest,
    StageCommand,
    apply_command,
    format_status,
    parse_datagram,
)


@dataclass
class FakeController:
    calls: list[tuple[object, ...]] = field(default_factory=list)

    def set_stepper(self, stepper: int, target: int) -> None:
        self.calls.append(("MOVE", stepper, target))

    def set_stepper_position(self, stepper: int, position: int) -> None:
        self.calls.append(("SET", stepper, position))

    def set_homing_state(self, stepper: int, state: bool, speed: int) -> None:
        self.calls.append(("HOME", stepper, state, speed))

    def get_stepper(self, stepper: int) -> int:
        return (1600, -25)[stepper]

    def get_home_position(self, stepper: int) -> int | None:
        return (None, 12)[stepper]

    def is_moving(self, stepper: int) -> bool:
        return (False, True)[stepper]

    def is_enabled(self) -> bool:
        return True

    def num_steppers(self) -> int:
        return 2


def test_connection_request_preserves_legacy_handshake() -> None:
    assert parse_datagram(b"REQ_CONN") == ConnectionRequest()


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (b"0,MOVE,0,1600", StageCommand("0", "MOVE", (0, 1600))),
        (b"1,SET,1,-25", StageCommand("1", "SET", (1, -25))),
        (b"2,HOME,0,T,100", StageCommand("2", "HOME", (0, True, 100))),
        (b"3,HOME,0,F,-100", StageCommand("3", "HOME", (0, False, -100))),
        (b"4,UNKNOWN", StageCommand("4", "UNKNOWN", ())),
    ],
)
def test_parse_datagram_preserves_legacy_commands(
    payload: bytes,
    expected: StageCommand,
) -> None:
    assert parse_datagram(payload) == expected


@pytest.mark.parametrize("payload", [b"", b"1", b"1,MOVE,0", b"1,HOME,0,T"])
def test_malformed_datagram_preserves_failure(payload: bytes) -> None:
    with pytest.raises((IndexError, ValueError)):
        parse_datagram(payload)


def test_apply_command_preserves_controller_calls() -> None:
    controller = FakeController()

    for payload in (b"0,MOVE,0,1600", b"1,SET,1,-25", b"2,HOME,0,T,100"):
        command = parse_datagram(payload)
        assert isinstance(command, StageCommand)
        apply_command(controller, command)

    assert controller.calls == [
        ("MOVE", 0, 1600),
        ("SET", 1, -25),
        ("HOME", 0, True, 100),
    ]


def test_format_status_matches_legacy_wire_format() -> None:
    assert format_status(FakeController(), "9") == (
        b"S,9;E,True;P,0,1600;M,0,False;H,0,None;"
        b"P,1,-25;M,1,True;H,1,12;"
    )