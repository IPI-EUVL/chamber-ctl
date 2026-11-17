import threading
import uuid
from types import SimpleNamespace

import pytest

from chamber_ctl.subsystems import euv_acquisition_controller as controller


def preinit_fixture():
    owner = object.__new__(controller.EuvAcquisitionSubsystem)
    run = SimpleNamespace(
        run_id=uuid.uuid4(), session_id=None, finalizing=False,
        source_kind='red_pitaya', source_id='red-pitaya',
        cadence=SimpleNamespace(reset=lambda _session: None),
        accumulator=SimpleNamespace(set_running=lambda _running: None),
    )
    owner._run = run
    owner._run_lock = threading.RLock()
    owner._capture_client = object()
    owner._pending_preinit = None
    owner._preinit_pending = False
    owner._start_pending = False
    owner._ExperimentClient__client_name = 'EUV Acquisition'
    owner._ExperimentClient__preinit_handle = None
    owner._ensure_capture_client = lambda **_kwargs: owner._capture_client
    owner._log = lambda *_args, **_kwargs: None
    owner._refresh_cadence_rate = lambda **_kwargs: None
    if hasattr(controller.EuvAcquisitionSubsystem, '_open_diagnostic_event_stream'):
        owner._open_diagnostic_event_stream = lambda _run: None
    owner._open_timing_event_stream = lambda _run: None
    owner._set_finalization_status = lambda *_args: None
    provenance = []
    owner._write_preinit_provenance = lambda session, run: provenance.append(session)
    failures, successes = [], []
    handle = SimpleNamespace(fail=failures.append, ret=successes.append)
    return owner, run, handle, provenance, failures, successes


def test_preinit_callback_queues_arming_until_lifecycle_handle_is_installed():
    owner, run, handle, provenance, failures, successes = preinit_fixture()
    commands = []
    owner._capture_command = lambda name, payload: commands.append(name) or {}
    assert isinstance(owner._on_preinit(handle), bytes)
    owner._process_pending_preinit()
    assert not commands
    owner._ExperimentClient__preinit_handle = handle
    owner._process_pending_preinit()
    assert commands == ['start_capture']
    assert provenance == [run.session_id]
    owner._complete_pending_lifecycle_events()
    assert successes == [b'Digitizer capture is armed.']
    assert not failures


def test_slow_arming_keeps_run_lock_free_and_honors_stop():
    owner, run, handle, provenance, failures, successes = preinit_fixture()
    entered, release = threading.Event(), threading.Event()
    commands = []

    def command(name, payload):
        commands.append(name)
        if name == 'start_capture':
            entered.set()
            assert release.wait(3)
        return {}

    owner._capture_command = command
    owner._on_preinit(handle)
    owner._ExperimentClient__preinit_handle = handle
    worker = threading.Thread(target=owner._process_pending_preinit)
    worker.start()
    try:
        assert entered.wait(1)
        assert owner._run_lock.acquire(timeout=0.2)
        owner._run_lock.release()
        assert owner._on_stop(None) == b'EUV acquisition is stopping.'
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive()
    assert run.finalizing
    assert commands == ['start_capture', 'stop_capture']
    assert failures and b'stopped while' in failures[0]
    assert not provenance and not successes and not owner._preinit_pending


def test_deferred_arming_failure_fails_original_handle_and_cleans_up():
    owner, run, handle, provenance, failures, successes = preinit_fixture()
    commands = []

    def command(name, payload):
        commands.append(name)
        if name == 'start_capture':
            raise TimeoutError('fixture arming failure')
        return {}

    owner._capture_command = command
    owner._on_preinit(handle)
    owner._ExperimentClient__preinit_handle = handle
    owner._process_pending_preinit()
    assert commands == ['start_capture', 'stop_capture']
    assert b'fixture arming failure' in failures[0]
    assert owner._ExperimentClient__preinit_handle is None
    assert not provenance and not successes


def test_stop_before_arming_ack_cannot_complete_preinit_successfully():
    owner, run, handle, provenance, failures, successes = preinit_fixture()
    owner._capture_command = lambda *_args: {}
    owner._on_preinit(handle)
    owner._ExperimentClient__preinit_handle = handle
    owner._process_pending_preinit()
    owner._on_stop(None)
    owner._complete_pending_lifecycle_events()
    assert failures and not successes
    assert owner._ExperimentClient__preinit_handle is None


def test_duplicate_preinit_does_not_replace_inflight_arming_request():
    owner, run, handle, provenance, failures, successes = preinit_fixture()
    owner._on_preinit(handle)
    assert owner._on_preinit(object())[0] is False
    assert owner._pending_preinit == (run, handle)
    owner._capture_command = lambda *_args: {}
    owner._ExperimentClient__preinit_handle = handle
    owner._process_pending_preinit()
    assert owner._on_preinit(object())[0] is False


@pytest.mark.parametrize('compact', [False, True])
def test_periodic_status_uses_compact_capability_without_changing_legacy_poll(compact):
    owner = object.__new__(controller.EuvAcquisitionSubsystem)
    commands = []
    owner._capture_client = SimpleNamespace(
        heartbeat_if_due=lambda: None,
        command=lambda *args: commands.append(args) or {},
    )
    owner._board_status = {'capabilities': {'compact_status': compact}}
    owner._next_board_status_monotonic = 0
    owner._cache_board_status = lambda value: None
    owner._maintain_capture_connection()
    assert commands == [('status', {'include_snapshots': False}) if compact else ('status',)]


def test_import_discards_only_pulse_totals_covered_by_persisted_snapshot(tmp_path):
    owner, _run, _handle, _provenance, _failures, _successes = preinit_fixture()
    snapshot_id = uuid.uuid4()
    manifest = SimpleNamespace(snapshot_id=snapshot_id, first_sequence=250, final_sequence=499)
    run = controller._AcquisitionRun(uuid.uuid4(), object(), None, None, 190, object())
    run.report_totals = {sequence: (float(sequence), sequence / 95) for sequence in range(750)}
    run.pending_snapshots[snapshot_id] = manifest
    owner._run = run
    owner._get_record = lambda _run_id: object()
    owner._artifact_importer = SimpleNamespace(import_snapshot=lambda *_args, **_kwargs: tmp_path / 'gone.h5')
    owner._segment_publisher = None
    owner._import_snapshot(manifest)
    assert set(run.report_totals) == set(range(250)) | set(range(500, 750))
    assert snapshot_id in run.imported_snapshot_ids


def test_pending_imports_yield_to_new_reports_after_one_artifact():
    owner = object.__new__(controller.EuvAcquisitionSubsystem)
    owner._run_lock = threading.RLock()
    manifests = [SimpleNamespace(snapshot_id=uuid.uuid4(), final_sequence=n) for n in (249, 499, 749)]
    owner._run = SimpleNamespace(
        pending_snapshots={item.snapshot_id: item for item in manifests},
        report_totals={item.final_sequence: (0.0, 0.0) for item in manifests},
    )
    imported = []
    owner._import_snapshot = imported.append
    owner._import_pending_snapshots()
    assert imported == manifests[:1]
