from dataclasses import replace
import uuid

from chamber_ctl.data.acquisition_preview import AcquisitionPreview
from chamber_ctl.gui.acquisition import (
    AcquisitionPreviewHistory,
    acquisition_control_state,
    acquisition_pipeline_status,
    acquisition_status_detail,
    acquisition_status_metrics,
    coalesce_acquisition_ui_updates,
    decode_acquisition_status,
)
from chamber_ctl.gui.central import ACQUISITION_WORKSPACE_TABS, CentralGUI


def test_acquisition_controls_are_gated_by_published_state_and_capabilities() -> None:
    idle_simulator = {
        "state": "idle",
        "capture_connected": True,
        "source_kind": "simulated",
        "capabilities": {"simulator_controls": True},
    }
    idle = acquisition_control_state(idle_simulator, dds_connected=True)
    exposure = acquisition_control_state(idle_simulator | {"state": "running"}, dds_connected=True)
    diagnostic = acquisition_control_state(
        idle_simulator | {"state": "diagnostic_running", "diagnostic_mode": "continuous"},
        dds_connected=True,
    )
    hardware = acquisition_control_state(idle_simulator | {"source_kind": "hardware"}, dds_connected=True)
    pulse_recovery = acquisition_control_state(
        idle_simulator | {"state": "running", "pulse_loss": True, "recovery_ready": True},
        dds_connected=True,
    )
    orphan_recovery = acquisition_control_state(
        idle_simulator | {"state": "recovery_required"},
        dds_connected=True,
    )

    assert idle.start_enabled and idle.one_shot_enabled
    assert not exposure.start_enabled and exposure.simulator_enabled
    assert diagnostic.flush_enabled and diagnostic.stop_enabled and diagnostic.simulator_enabled
    assert not hardware.simulator_enabled
    assert pulse_recovery.authorize_recovery_enabled
    assert idle.recover_orphan_enabled
    assert orphan_recovery.recover_orphan_enabled
    assert not acquisition_control_state(idle_simulator, dds_connected=False).start_enabled


def test_central_gui_registers_an_acquisition_tab_builder() -> None:
    assert hasattr(CentralGUI, "_CentralGUI__build_acquisition_tab")
    assert not hasattr(CentralGUI, "_CentralGUI__build_exposure_tab")
    assert ACQUISITION_WORKSPACE_TABS == ("Exposure", "Capture Diagnostics")


def test_status_decoder_accepts_dds_byte_lists() -> None:
    status = decode_acquisition_status(list(b'{"state":"idle"}'))

    assert status == {"state": "idle"}


def test_ui_updates_keep_only_latest_status_and_cadence() -> None:
    updates = [
        ("status", {"sequence": 1}),
        ("preview", "first-preview"),
        ("cadence", "first-cadence"),
        ("result", ("stop", "complete")),
        ("status", {"sequence": 2}),
        ("preview", "second-preview"),
        ("cadence", "latest-cadence"),
    ]

    assert coalesce_acquisition_ui_updates(updates) == (
        ("preview", "first-preview"),
        ("result", ("stop", "complete")),
        ("preview", "second-preview"),
        ("status", {"sequence": 2}),
        ("cadence", "latest-cadence"),
    )


def test_acquisition_status_formats_live_metrics_and_transferred_snapshots() -> None:
    status = {
        "state": "diagnostic_running",
        "diagnostic_mode": "one_shot",
        "diagnostic_report_count": 1,
        "processed_snapshot_count": 1,
        "pending_snapshot_count": 0,
        "accumulated_dose_mj_cm2": 2.5,
        "transmitting_runtime_seconds": 0.0,
    }

    assert acquisition_status_metrics(status) == ("2.50 mJ/cm2", "0.00 s")
    assert acquisition_status_detail(status) == (
        "one-shot; 1 pulse report(s); 1 snapshot(s) transferred; 0 pending"
    )


def test_pipeline_status_formats_mode_rate_queues_timings_and_fault() -> None:
    status = {
        "pipeline_metrics": {
            "capture_mode": {
                "requested": "auto",
                "effective": "single-shot",
                "fallback_reason": "AXI unavailable",
            },
            "capture_worker": {
                "pid": 123,
                "cpu": 1,
                "scheduler": "fifo",
                "realtime_priority": 20,
            },
            "counters": {"accepted": 192},
            "elapsed_seconds": 2.0,
            "queues": {
                "capture": {"depth": 2, "capacity": 64, "high_water": 5},
                "persistence": {"depth": 0, "capacity": 8, "high_water": 1},
                "control": {"depth": 3, "capacity": 256, "high_water": 7},
            },
            "stages": {
                "hardware_read": {"p95_ms": 0.25},
                "analysis": {"p95_ms": 1.5},
                "trigger_to_report": {"p95_ms": 2.75},
            },
            "terminal_error": "control queue overflow",
        }
    }

    pipeline = acquisition_pipeline_status(status)

    assert pipeline.mode == "single-shot (requested auto)"
    assert pipeline.worker == "PID 123; CPU 1; FIFO 20"
    assert pipeline.fallback == "AXI unavailable"
    assert pipeline.accepted_rate == "96.0 Hz (192 total)"
    assert pipeline.queues == (
        "capture 2/64 (high 5) | persistence 0/8 (high 1) | control 3/256 (high 7)"
    )
    assert pipeline.timings == "read 0.25 ms | analysis 1.50 ms | trigger-report 2.75 ms"
    assert pipeline.fault == "control queue overflow"


def test_pipeline_status_is_backward_compatible_when_metrics_are_missing() -> None:
    pipeline = acquisition_pipeline_status({"state": "idle"})

    assert set(vars(pipeline).values()) == {"N/A"}


def test_idle_status_keeps_the_last_diagnostic_transfer_summary_visible() -> None:
    status = {
        "state": "idle",
        "last_diagnostic": {
            "mode": "one_shot",
            "report_count": 1,
            "snapshot_count": 1,
        },
    }

    assert acquisition_status_detail(status) == (
        "Last one-shot test: 1 pulse report(s); 1 snapshot(s) transferred"
    )


def test_preview_history_caps_items_and_resets_for_a_new_session() -> None:
    import numpy as np

    session_id = uuid.uuid4()
    base = AcquisitionPreview(
        context="diagnostic",
        run_id=None,
        session_id=session_id,
        snapshot_id=uuid.uuid4(),
        source_kind="simulated",
        source_id="fixture",
        close_reason="explicit_flush",
        first_sequence=0,
        final_sequence=0,
        total_pulse_count=1,
        first_capture_unix_ns=1,
        final_capture_unix_ns=1,
        sample_rate_hz=1_000_000.0,
        window_seconds=2e-6,
        pretrigger_seconds=1e-6,
        samples_v=np.zeros((1, 2), dtype=np.float32),
        sequence=np.asarray([0], dtype=np.uint64),
        captured_at_unix_ns=np.asarray([1], dtype=np.int64),
        quality=np.asarray([0], dtype=np.uint32),
    )
    history = AcquisitionPreviewHistory(limit=2)
    history.append(base)
    history.append(replace(base, snapshot_id=uuid.uuid4()))
    history.append(replace(base, snapshot_id=uuid.uuid4()))

    assert len(history) == 2
    new_session = replace(base, session_id=uuid.uuid4(), snapshot_id=uuid.uuid4())
    assert history.append(new_session) == 0
    assert len(history) == 1
    assert history.session_id == new_session.session_id