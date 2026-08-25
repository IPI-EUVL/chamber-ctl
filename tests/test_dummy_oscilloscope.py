import time

import numpy as np

from chamber_ctl.subsystems.laser import LaserSyncStatus
from chamber_ctl.subsystems.oscilloscope import DummyOscilloscope
from chamber_ctl.subsystems import uuids


def _status(*, current_phase, configured_target_phase=10.0, preinit_phase=0.0):
    return LaserSyncStatus(
        laser_on=True,
        laser_warming_up=False,
        chopper_on=True,
        chopper_starting_up=False,
        current_phase=current_phase,
        target_phase=current_phase,
        preinit_phase=preinit_phase,
        configured_target_phase=configured_target_phase,
    )


def test_dummy_scope_emits_pulses_only_at_the_configured_open_phase() -> None:
    scope = DummyOscilloscope()

    _time, closed_waveform, _timestamp = scope.dummy_wf()
    scope._DummyOscilloscope__on_laser_status(_status(current_phase=0.0).encode())
    _time, preinit_waveform, _timestamp = scope.dummy_wf()
    scope._DummyOscilloscope__on_laser_status(_status(current_phase=10.0).encode())
    _time, open_waveform, _timestamp = scope.dummy_wf()

    assert np.ptp(closed_waveform) == 0.0
    assert np.ptp(preinit_waveform) == 0.0
    assert np.ptp(open_waveform) > 0.3


def test_dummy_scope_fails_closed_for_stale_or_incomplete_status() -> None:
    scope = DummyOscilloscope()
    scope._DummyOscilloscope__on_laser_status(_status(current_phase=10.0).encode())
    scope._DummyOscilloscope__laser_status_received_at = time.monotonic() - scope.STATUS_MAX_AGE_SECONDS - 0.01

    _time, stale_waveform, _timestamp = scope.dummy_wf()
    scope._DummyOscilloscope__on_laser_status(
        LaserSyncStatus(True, False, True, False, 10.0, 10.0).encode()
    )
    _time, incomplete_waveform, _timestamp = scope.dummy_wf()

    assert np.ptp(stale_waveform) == 0.0
    assert np.ptp(incomplete_waveform) == 0.0


def test_dummy_scope_registers_a_temporary_laser_status_subscriber() -> None:
    class _RemoteKv:
        def __init__(self) -> None:
            self.callback = None

        def on_new_data_received(self, callback) -> None:
            self.callback = callback

    class _Handle:
        def __init__(self) -> None:
            self.remote_calls = []
            self.remote_kv = _RemoteKv()

        def add_remote_kv(self, subsystem_uuid, descriptor):
            self.remote_calls.append((subsystem_uuid, descriptor))
            return self.remote_kv

    class _Client:
        def __init__(self) -> None:
            self.calls = []
            self.handle = _Handle()

        def register_subsystem(self, name, subsystem_uuid, temporary=False):
            self.calls.append((name, subsystem_uuid, temporary))
            return self.handle

    scope = DummyOscilloscope()
    dds_client = _Client()
    scope._DummyOscilloscope__dds_client = dds_client

    scope._DummyOscilloscope__on_dds_ready()

    assert dds_client.calls[0][0] == "DummyScope"
    assert dds_client.calls[0][2] is True
    assert dds_client.handle.remote_calls[0][0] == uuids.UUID_LASER_CONTROLLER
    assert dds_client.handle.remote_kv.callback is not None


def test_dummy_scope_one_shot_capture_runs_while_inactive() -> None:
    class _OneIterationStopFlag:
        def __init__(self) -> None:
            self.calls = 0

        def run(self) -> bool:
            self.calls += 1
            return self.calls == 1

    scope = DummyOscilloscope()
    timestamps = [time.time_ns()]
    scope._DummyOscilloscope__do_capture_once = True
    scope.dummy_wfs = lambda _count: (
        [np.array([0.0, 1e-6])],
        [np.array([0.0, 0.4])],
        {},
        timestamps,
        None,
    )

    scope._DummyOscilloscope__proc_thread(_OneIterationStopFlag())
    start, end, data, indexes, _uid = scope.get_out_queue().get_nowait()

    assert start <= end
    assert data.shape == (2, 2)
    assert indexes == [(0, timestamps[0] / 1e9)]