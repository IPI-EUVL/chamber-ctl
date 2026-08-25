import argparse

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