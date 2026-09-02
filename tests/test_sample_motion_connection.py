from __future__ import annotations

import queue
from dataclasses import dataclass

import pytest

import chamber_ctl.subsystems.sample_motion as sample_motion


class _InertThread:
    def __init__(self, *, target, daemon: bool) -> None:
        self.target = target
        self.daemon = daemon

    def start(self) -> None:
        pass


class _FakeSocket:
    def __init__(self) -> None:
        self.send_error: OSError | None = None
        self.receive_error: OSError | None = None
        self.received = b"P,0,12;P,1,34;M,0,False;M,1,False;H,0,None;E,False"
        self.sent: list[bytes] = []

    def sendto(self, data: bytes, _address: tuple[str, int]) -> int:
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(data)
        return len(data)

    def recvfrom(self, _size: int) -> tuple[bytes, tuple[str, int]]:
        if self.receive_error is not None:
            raise self.receive_error
        return self.received, ("stage", 11755)

    def bind(self, _address: tuple[str, int]) -> None:
        pass

    def close(self) -> None:
        pass


def _make_client(monkeypatch: pytest.MonkeyPatch) -> tuple[sample_motion.StepperClient, _FakeSocket]:
    fake_socket = _FakeSocket()
    monkeypatch.setattr(sample_motion.socket, "socket", lambda *_args: fake_socket)
    monkeypatch.setattr(sample_motion.threading, "Thread", _InertThread)
    return sample_motion.StepperClient(11756, ("stage", 11755)), fake_socket


def test_client_defaults_are_safe_before_first_stage_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = _make_client(monkeypatch)

    assert client.get_position(0) == 0
    assert client.get_position(1) == 0
    assert client.get_home(0) is None
    assert not client.is_moving()
    with pytest.raises(ConnectionError, match="waiting for sample stage response"):
        client.queue_move(0, 10)


def test_socket_failure_stays_retryable_and_valid_reply_clears_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, fake_socket = _make_client(monkeypatch)
    fake_socket.send_error = OSError("network unreachable")

    client._StepperClient__send_data()

    assert not client.is_online()
    assert "network unreachable" in client.get_connection_error()
    with pytest.raises(ConnectionError, match="network unreachable"):
        client.wait_ack(timeout=0.01)

    fake_socket.send_error = None
    client._StepperClient__receive()

    assert client.is_online()
    assert client.get_connection_error() is None
    assert client.get_position(0) == 12


def test_receive_reset_marks_offline_without_killing_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, fake_socket = _make_client(monkeypatch)
    client._StepperClient__receive()
    fake_socket.receive_error = ConnectionResetError("port unreachable")

    client._StepperClient__receive()

    assert not client.is_online()
    assert client.get_connection_error() == "receive failed: port unreachable"

    fake_socket.receive_error = None
    client._StepperClient__receive()

    assert client.is_online()
    assert client.get_connection_error() is None


def test_interrupted_command_is_not_replayed_after_connection_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, fake_socket = _make_client(monkeypatch)
    client._StepperClient__receive()
    client.queue_move(0, 25)
    fake_socket.send_error = OSError("network unreachable")

    client._StepperClient__send_data()

    fake_socket.send_error = None
    client._StepperClient__receive()
    client._StepperClient__send_data()

    assert fake_socket.sent == []
    with pytest.raises(ConnectionError, match="command discarded"):
        client.wait_ack(timeout=0.01)


def test_connection_timeout_discards_commands_before_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [100.0]
    monkeypatch.setattr(sample_motion.time, "time", lambda: now[0])
    client, fake_socket = _make_client(monkeypatch)
    client._StepperClient__receive()
    client.queue_move(0, 25)

    now[0] += sample_motion.CONNECTION_TIMEOUT_SECONDS

    assert not client.is_online()
    assert "timed out" in client.get_connection_error()

    client._StepperClient__receive()
    client._StepperClient__send_data()

    assert fake_socket.sent == []
    with pytest.raises(ConnectionError, match="command discarded"):
        client.wait_ack(timeout=0.01)


class _OfflineClient:
    def is_online(self) -> bool:
        return False

    def get_connection_error(self) -> str:
        return "receive failed: network unreachable"

    def raise_if_offline(self) -> None:
        raise ConnectionError("Sample stage offline: network unreachable")


def test_pi_stage_reports_offline_during_an_active_operation() -> None:
    controller = object.__new__(sample_motion.PiStageController)
    controller._PiStageController__client = _OfflineClient()
    controller._PiStageController__state = sample_motion.STATE_MOVING

    assert controller.get_state() == sample_motion.STATE_OFFLINE
    assert controller.get_connection_error() == "receive failed: network unreachable"


def test_pi_stage_rejects_new_motion_while_offline() -> None:
    controller = object.__new__(sample_motion.PiStageController)
    controller._PiStageController__client = _OfflineClient()
    controller._PiStageController__opqueue = queue.Queue()

    with pytest.raises(ConnectionError, match="network unreachable"):
        controller.move_to(1.0, 2.0)

    assert controller._PiStageController__opqueue.empty()


def test_pi_stage_wait_idle_aborts_while_offline() -> None:
    controller = object.__new__(sample_motion.PiStageController)
    controller._PiStageController__client = _OfflineClient()
    controller._PiStageController__state = sample_motion.STATE_MOVING

    with pytest.raises(ConnectionError, match="network unreachable"):
        controller.wait_idle()


@dataclass
class _StatusItem:
    STATE_INFO = 0
    STATE_WARN = 1
    STATE_ALARM = 2

    severity: int
    code: int
    message: str


class _OfflineStage:
    def __init__(self) -> None:
        self.state = sample_motion.STATE_OFFLINE
        self.connection_error: str | None = "connection timed out"

    def get_state(self) -> int:
        return self.state

    def get_connection_error(self) -> str | None:
        return self.connection_error

    def get_position(self) -> tuple[float, float]:
        return (0.0, 0.0)

    def get_slot_count(self) -> int:
        return 0

    def is_linear_homed(self) -> bool:
        return True

    def get_homing_error(self) -> None:
        return None


class _StatusSink:
    def __init__(self) -> None:
        self.items: list[_StatusItem] = []
        self.active: dict[int, _StatusItem] = {}
        self.cleared: list[int] = []

    def put_status_item(self, item: _StatusItem) -> None:
        self.items.append(item)
        self.active[item.code] = item

    def get_status_item_exists(self, code: int) -> bool:
        return code in self.active

    def clear_status_item(self, code: int) -> None:
        self.active.pop(code, None)
        self.cleared.append(code)


def test_offline_status_is_an_alarm_with_connection_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sample_motion, "StatusItem", _StatusItem)
    sink = _StatusSink()
    stage = _OfflineStage()
    subsystem = object.__new__(sample_motion.SampleMotionSubsystem)
    subsystem._SampleMotionSubsystem__stage = stage
    subsystem._SampleMotionSubsystem__subsystem = sink
    subsystem._SampleMotionSubsystem__status_item_cache = {}

    subsystem._SampleMotionSubsystem__update_status_items()

    offline = next(item for item in sink.items if item.code == 100)
    assert offline.severity == _StatusItem.STATE_ALARM
    assert offline.message == "Sample stage offline: connection timed out"

    stage.state = sample_motion.STATE_IDLE
    stage.connection_error = None
    subsystem._SampleMotionSubsystem__update_status_items()

    assert 100 in sink.cleared
    assert 100 not in sink.active