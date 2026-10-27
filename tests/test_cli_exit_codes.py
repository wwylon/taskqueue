"""8.1 HIGH: no-arg commands reject stray args (exit 2) instead of acting; failures return non-zero."""
from __future__ import annotations

from pathlib import Path

import pytest

from taskqueue import cli


@pytest.mark.parametrize("fn", [cli._cmd_start, cli._cmd_stop_daemon, cli._cmd_doctor])
def test_no_arg_commands_reject_args(fn, tmp_path: Path, monkeypatch, capsys) -> None:
    called: list[str] = []
    for name in ("cmd_start", "cmd_stop_daemon", "cmd_doctor"):
        monkeypatch.setattr(cli, name, lambda *a, _n=name, **k: called.append(_n))
    assert fn(tmp_path, ["--help"]) == 2
    assert called == []
    assert "Usage" in capsys.readouterr().out


@pytest.mark.parametrize("fn", [cli._cmd_pause, cli._cmd_resume, cli._cmd_freeze, cli._cmd_unfreeze])
def test_project_commands_reject_flags(fn, tmp_path: Path, monkeypatch) -> None:
    for name in ("cmd_pause", "cmd_resume_project", "cmd_freeze", "cmd_unfreeze"):
        monkeypatch.setattr(cli, name, lambda *a, **k: pytest.fail("must not act on a flag"))
    assert fn(tmp_path, ["--help"]) == 2
    assert fn(tmp_path, ["a", "b"]) == 2


def test_start_failure_exit_code(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(cli, "cmd_start", lambda home: 1)
    assert cli._cmd_start(tmp_path, []) == 1


def test_retry_project_bad_status_filter_is_usage_error(tmp_path: Path) -> None:
    from taskqueue._cli_project import cmd_retry_project

    assert cmd_retry_project(tmp_path, "p", status_filter="bogus") == 2


@pytest.mark.parametrize("name", ["cmd_pause", "cmd_resume_project", "cmd_freeze", "cmd_unfreeze"])
def test_project_commands_unknown_project_exit_1(name, tmp_path: Path, capsys) -> None:
    from taskqueue import _cli_project

    assert getattr(_cli_project, name)(tmp_path, "nope") == 1
    assert "Unknown project" in capsys.readouterr().err


def test_stop_daemon_and_restart_refuse_while_tasks_run(tmp_path: Path, monkeypatch, capsys) -> None:
    from taskqueue import _cli_admin

    monkeypatch.setattr(_cli_admin, "_daemon_running", lambda home: 4242)
    monkeypatch.setattr(_cli_admin, "_running_runs", lambda home: ["p/a", "p/b"])
    monkeypatch.setattr(_cli_admin.os, "kill", lambda *a: pytest.fail("must not signal"))
    assert _cli_admin.cmd_stop_daemon(tmp_path) == 1
    assert _cli_admin.cmd_restart(tmp_path) == 1
    out = capsys.readouterr().out
    assert "Refusing" in out and "p/a" in out and "--force" in out


def test_stop_daemon_force_signals(tmp_path: Path, monkeypatch) -> None:
    import sys
    from taskqueue import _cli_admin

    sent: list[int] = []
    monkeypatch.setattr(_cli_admin, "_daemon_running", lambda home: 4242)
    monkeypatch.setattr(_cli_admin, "_running_runs", lambda home: ["p/a"])
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(_cli_admin.os, "kill", lambda pid, sig: sent.append(pid))
    assert _cli_admin.cmd_stop_daemon(tmp_path, force=True) == 0
    assert sent == [4242]


def test_running_runs_missing_db(tmp_path: Path) -> None:
    from taskqueue import _cli_admin

    assert _cli_admin._running_runs(tmp_path) == []


def test_priority_range_and_unknown_task(tmp_path, capsys):
    from taskqueue import cli
    from taskqueue.db import open_db

    open_db(tmp_path / "queue.db").close()
    assert cli._cmd_priority(tmp_path, ["p/t", "101"]) == 2
    assert cli._cmd_priority(tmp_path, ["p/t", "x"]) == 2
    assert cli._cmd_priority(tmp_path, ["p/t"]) == 2
    assert cli._cmd_priority(tmp_path, ["p/missing", "50"]) == 1
    assert "Unknown task" in capsys.readouterr().err


def test_help_never_runs_the_command(tmp_path, monkeypatch, capsys):
    from taskqueue import cli

    ran = []
    monkeypatch.setitem(cli.COMMANDS, "start", lambda h, r: ran.append(r) or 0)
    monkeypatch.setattr(cli, "_home", lambda: tmp_path)
    assert cli.main(["start", "--help"]) == 0
    assert ran == [] and "tq start" in capsys.readouterr().out


def test_unknown_flag_and_command_are_usage_errors(tmp_path, monkeypatch, capsys):
    from taskqueue import cli

    ran = []
    monkeypatch.setitem(cli.COMMANDS, "status", lambda h, r: ran.append(r) or 0)
    monkeypatch.setattr(cli, "_home", lambda: tmp_path)
    assert cli.main(["status", "--jsno"]) == 2
    assert "--jsno" in capsys.readouterr().err and ran == []
    assert cli.main(["status", "--json", "--project=p"]) == 0 and ran == [["--json", "--project=p"]]
    assert cli.main(["no-such-command"]) == 2


def test_uninstall_service_reports_schtasks_failure(tmp_path, monkeypatch, capsys):
    import subprocess as _sp
    import sys as _sys

    from taskqueue import worker

    monkeypatch.setattr(_sys, "platform", "win32")
    monkeypatch.setattr(worker.subprocess, "run", lambda *a, **k: _sp.CompletedProcess(a, 1, "", "ERROR: Access is denied."))
    assert worker.uninstall_worker_service(tmp_path, "w1") == 1
    assert "Access is denied" in capsys.readouterr().err
