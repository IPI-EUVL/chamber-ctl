import argparse
import json

import pytest

from chamber_ctl.cli import acquisition


def test_recover_orphan_requires_explicit_confirmation(monkeypatch) -> None:
    monkeypatch.setattr(acquisition, "_call_acquisition_event", lambda *_args: (_ for _ in ()).throw(AssertionError()))

    with pytest.raises(ValueError, match="--confirm"):
        acquisition.main(argparse.Namespace(acquisition_command="recover-orphan", confirm=False, timeout_seconds=1.0))


def test_cli_sends_the_expected_recovery_events(monkeypatch, capsys) -> None:
    calls = []
    monkeypatch.setattr(acquisition, "_call_acquisition_event", lambda *args: calls.append(args) or "completed")

    assert acquisition.main(argparse.Namespace(acquisition_command="resume-interlock", timeout_seconds=1.0)) == 0
    assert acquisition.main(argparse.Namespace(acquisition_command="recover-orphan", confirm=True, timeout_seconds=2.0)) == 0

    assert calls == [
        (b"resume_acquisition_interlock", b"", 1.0),
        (b"recover_orphaned_capture_session", b"confirm", 2.0),
    ]
    assert capsys.readouterr().out == "completed\ncompleted\n"


def test_cli_routes_diagnostics_and_simulator_controls_through_dds(monkeypatch, capsys) -> None:
    calls = []
    monkeypatch.setattr(acquisition, "_call_acquisition_event", lambda *args: calls.append(args) or "completed")

    for command, timeout in (
        ("test-start", 1.0),
        ("test-one-shot", 2.0),
        ("test-flush", 3.0),
        ("test-stop", 4.0),
    ):
        assert acquisition.main(argparse.Namespace(acquisition_command=command, timeout_seconds=timeout)) == 0
    assert acquisition.main(
        argparse.Namespace(
            acquisition_command="simulator-set",
            name="pll_locked",
            state="off",
            timeout_seconds=5.0,
        )
    ) == 0
    assert acquisition.main(argparse.Namespace(acquisition_command="simulator-restore", timeout_seconds=6.0)) == 0

    assert calls[:4] == [
        (b"acquisition_test_start", b"", 1.0),
        (b"acquisition_test_one_shot", b"", 2.0),
        (b"acquisition_test_flush", b"", 3.0),
        (b"acquisition_test_stop", b"", 4.0),
    ]
    assert calls[4][0] == b"set_acquisition_simulator_control"
    assert json.loads(calls[4][1]) == {"name": "pll_locked", "enabled": False}
    assert calls[4][2] == 5.0
    assert calls[5] == (b"restore_acquisition_simulator_controls", b"", 6.0)
    assert capsys.readouterr().out == "completed\n" * 6