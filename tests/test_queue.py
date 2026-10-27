"""Tests for taskqueue core — db, runner, multi-project scheduling."""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from taskqueue.config import Project
from taskqueue._db_tasks import (
    get_all_tasks,
    get_queued_tasks,
    get_task,
    retry_task,
    set_task_status,
    upsert_task,
)
from taskqueue.db import open_db, parse_task_yaml
from taskqueue.models import STATUS_COMPLETED, STATUS_FAILED, STATUS_QUEUED
from tests.helpers.wait import wait_until


def _project(name: str, root: Path, queue_dir: Path | None = None) -> Project:
    return Project(
        name=name,
        root=root,
        queue_dir=queue_dir or root / "experiments" / "operator_queue",
    )


def _yaml(tmp_path: Path, project_name: str, task_id: str, **kwargs) -> Path:
    defaults = {
        "command": sys.executable,
        "args": ["-c", "print('hello')"],
        "resources": {"cpu_cores": 1},
        "priority": 50,
        "enabled": True,
    }
    defaults.update(kwargs)
    import yaml
    content = yaml.dump({"task_id": task_id, **defaults})
    p = tmp_path / f"{project_name}_{task_id}.yaml"
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# parse_task_yaml
# ---------------------------------------------------------------------------

def test_parse_task_yaml_basic(tmp_path: Path) -> None:
    proj = _project("pokerbot", tmp_path)
    path = _yaml(tmp_path, "pokerbot", "T5.3", args=["-c", "pass"])
    task = parse_task_yaml(path, proj)
    assert task.project == "pokerbot"
    assert task.task_id == "T5.3"
    assert task.args == ("-c", "pass")
    assert task.enabled is True


def test_parse_task_yaml_resume_args(tmp_path: Path) -> None:
    proj = _project("pimc", tmp_path)
    path = _yaml(tmp_path, "pimc", "bench_01",
                 resume_args=["-c", "pass", "--resume"])
    task = parse_task_yaml(path, proj)
    assert task.resume_args == ("-c", "pass", "--resume")


def test_parse_task_yaml_rejects_slash_in_task_id(tmp_path: Path) -> None:
    import yaml
    proj = _project("x", tmp_path)
    p = tmp_path / "bad.yaml"
    p.write_text(yaml.dump({"task_id": "foo/bar", "command": sys.executable}))
    with pytest.raises(ValueError, match="must not contain"):
        parse_task_yaml(p, proj)


def test_parse_task_yaml_requires_task_id(tmp_path: Path) -> None:
    import yaml
    proj = _project("x", tmp_path)
    p = tmp_path / "bad.yaml"
    p.write_text(yaml.dump({"command": sys.executable}))
    with pytest.raises(KeyError):
        parse_task_yaml(p, proj)


# ---------------------------------------------------------------------------
# DB — two projects, composite key
# ---------------------------------------------------------------------------

def test_two_projects_independent(tmp_path: Path) -> None:
    """Tasks with the same task_id in different projects are independent."""
    db = open_db(tmp_path / "queue.db")
    proj_a = _project("alpha", tmp_path / "alpha")
    proj_b = _project("beta", tmp_path / "beta")

    yaml_a = _yaml(tmp_path, "alpha", "task1")
    yaml_b = _yaml(tmp_path, "beta", "task1")

    upsert_task(db, parse_task_yaml(yaml_a, proj_a))
    upsert_task(db, parse_task_yaml(yaml_b, proj_b))

    row_a = get_task(db, "alpha", "task1")
    row_b = get_task(db, "beta", "task1")
    assert row_a is not None and row_b is not None
    assert row_a["project"] == "alpha"
    assert row_b["project"] == "beta"


def test_upsert_does_not_requee_completed(tmp_path: Path) -> None:
    db = open_db(tmp_path / "queue.db")
    proj = _project("p", tmp_path)
    path = _yaml(tmp_path, "p", "job1")
    upsert_task(db, parse_task_yaml(path, proj))
    set_task_status(db, "p", "job1", STATUS_COMPLETED)
    upsert_task(db, parse_task_yaml(path, proj))  # re-sync
    row = get_task(db, "p", "job1")
    assert row["status"] == STATUS_COMPLETED  # must not revert to queued


def test_disabled_task_skipped(tmp_path: Path) -> None:
    db = open_db(tmp_path / "queue.db")
    proj = _project("p", tmp_path)
    path = _yaml(tmp_path, "p", "disabled_job", enabled=False)
    upsert_task(db, parse_task_yaml(path, proj))
    queued = get_queued_tasks(db)
    assert not any(r["task_id"] == "disabled_job" for r in queued)


def test_retry_task(tmp_path: Path) -> None:
    db = open_db(tmp_path / "queue.db")
    proj = _project("p", tmp_path)
    path = _yaml(tmp_path, "p", "fail_job")
    upsert_task(db, parse_task_yaml(path, proj))
    set_task_status(db, "p", "fail_job", STATUS_FAILED)
    assert retry_task(db, "p", "fail_job") is True
    assert get_task(db, "p", "fail_job")["status"] == STATUS_QUEUED


def test_filter_by_project(tmp_path: Path) -> None:
    db = open_db(tmp_path / "queue.db")
    for proj_name in ("alpha", "beta", "gamma"):
        proj = _project(proj_name, tmp_path / proj_name)
        path = _yaml(tmp_path, proj_name, "taskX")
        upsert_task(db, parse_task_yaml(path, proj))

    all_tasks = get_all_tasks(db)
    beta_tasks = get_all_tasks(db, project="beta")
    assert len(all_tasks) == 3
    assert len(beta_tasks) == 1
    assert beta_tasks[0]["project"] == "beta"


# ---------------------------------------------------------------------------
# Runner — end-to-end subprocess
# ---------------------------------------------------------------------------

def test_run_success(tmp_path: Path) -> None:
    from taskqueue.db import get_run, get_runs_for_task, insert_run
    from taskqueue.runner import ActiveRun

    db = open_db(tmp_path / "queue.db")
    proj = _project("p", tmp_path)
    script = tmp_path / "ok.py"
    script.write_text("print('(50.0%) halfway'); print('done')")
    path = _yaml(tmp_path, "p", "run_ok", **{
        "command": sys.executable,
        "args": [str(script)],
    })
    task = parse_task_yaml(path, proj)
    upsert_task(db, task)
    row = get_task(db, "p", "run_ok")

    run = ActiveRun(conn=db, task_row=row, log_dir=tmp_path / "logs", cwd=tmp_path)
    assert run.start(use_resume=False) is None

    wait_until(run.poll, timeout=30, interval=0.05)
    assert run.poll(), "process did not finish in time"

    run.finalize(STATUS_COMPLETED)
    assert get_task(db, "p", "run_ok")["status"] == STATUS_COMPLETED
    assert run.progress_pct == 50.0


def test_utf8_stdout_does_not_crash_reader_thread(tmp_path: Path) -> None:
    """Regression: subprocess.Popen(text=True, ...) without an explicit
    encoding decodes the child's stdout using the OS default codepage
    (cp1252 on this host), not UTF-8. A task printing non-ASCII UI strings
    (Arabic translations) hit a byte sequence cp1252 can't map at all
    (0x81), raising UnicodeDecodeError inside the reader thread and killing
    it silently -- see test_stream_output_recovers_from_reader_crash for
    the "already crashed" recovery path this also guards against. This
    test instead asserts the crash doesn't happen in the first place, and
    that the Arabic text round-trips correctly through the log."""
    from taskqueue.runner import ActiveRun

    db = open_db(tmp_path / "queue.db")
    proj = _project("p", tmp_path)
    script = tmp_path / "arabic.py"
    script.write_text(
        "import sys; sys.stdout.reconfigure(encoding='utf-8')\n"
        "print('(50.0%) \\u0645\\u0631\\u062d\\u0628\\u064b\\u0627 \\u0628\\u0627\\u0644\\u0639\\u0627\\u0644\\u0645')\n"
        "print('done')\n",
        encoding="utf-8",
    )
    path = _yaml(tmp_path, "p", "run_arabic", **{
        "command": sys.executable,
        "args": [str(script)],
    })
    task = parse_task_yaml(path, proj)
    upsert_task(db, task)
    row = get_task(db, "p", "run_arabic")

    run = ActiveRun(conn=db, task_row=row, log_dir=tmp_path / "logs", cwd=tmp_path)
    assert run.start(use_resume=False) is None

    wait_until(run.poll, timeout=30, interval=0.05)
    assert run.poll(), "process did not finish in time (reader thread likely crashed on decode)"
    assert run._exit_code == 0
    assert run.progress_pct == 50.0

    log_files = list((tmp_path / "logs").glob("run_arabic_*.log"))
    assert log_files, "expected a log file to have been written"
    log_content = log_files[0].read_text(encoding="utf-8")
    assert "مرحبًا" in log_content, f"Arabic text should round-trip as UTF-8, not mojibake: {log_content!r}"


def test_stream_output_recovers_from_reader_crash(tmp_path: Path, monkeypatch) -> None:
    """If _stream_output crashes mid-read for any reason, the run must still
    finalize instead of wedging in "running" forever (nothing else sets
    self._done: poll() never returns True, finalize() never runs, and
    stop_requested is never cleared -- this is exactly what happened in
    production before the fix)."""
    from taskqueue.runner import ActiveRun

    monkeypatch.setattr("taskqueue.reap.kill_process_tree", lambda *_a, **_k: None)

    db = open_db(tmp_path / "queue.db")
    proj = _project("p", tmp_path)
    path = _yaml(tmp_path, "p", "crash_test", **{
        "command": sys.executable,
        "args": ["-c", "print('hi')"],
    })
    task = parse_task_yaml(path, proj)
    upsert_task(db, task)
    row = get_task(db, "p", "crash_test")

    run = ActiveRun(conn=db, task_row=row, log_dir=tmp_path / "logs", cwd=tmp_path)

    class _StdoutThatCrashesMidStream:
        def __init__(self) -> None:
            self._yielded_first = False

        def __iter__(self):
            return self

        def __next__(self):
            if not self._yielded_first:
                self._yielded_first = True
                return "(10.0%) partial progress\n"
            raise UnicodeDecodeError("cp1252", b"\x81", 0, 1, "character maps to <undefined>")

    class _FakeProc:
        def __init__(self) -> None:
            self.stdout = _StdoutThatCrashesMidStream()
            self.returncode = None
            self.pid = 424242

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

        def wait(self, timeout=None):
            return self.returncode

    run.proc = _FakeProc()
    run._log_file = open(tmp_path / "crash.log", "w", encoding="utf-8")
    run.started_at = time.monotonic()

    run._stream_output()  # exercised synchronously; normally run on a thread

    assert run.poll(), "a crash inside _stream_output must still result in the run being finalized"
    assert run._exit_code is not None
    assert run.progress_pct == 10.0, "progress recorded before the crash should be preserved"


def test_stop_recovers_when_reader_thread_is_wedged(tmp_path: Path) -> None:
    """Regression: on Windows, a subprocess's stdout reader thread has been
    observed to not notice EOF promptly after the process is killed, which
    wedges the run in "running" forever -- stop() returns instantly once
    proc.poll() is non-None, but nothing ever sets self._done, so the
    scheduler loop re-calls stop() every tick and stop_requested is never
    cleared (finalize() is gated on run.poll()). stop() must recover within
    a bounded time instead of hanging indefinitely."""
    from taskqueue.runner import ActiveRun

    db = open_db(tmp_path / "queue.db")
    proj = _project("p", tmp_path)
    path = _yaml(tmp_path, "p", "wedge_test", **{
        "command": sys.executable,
        "args": ["-c", "print('hi')"],
    })
    task = parse_task_yaml(path, proj)
    upsert_task(db, task)
    row = get_task(db, "p", "wedge_test")

    run = ActiveRun(conn=db, task_row=row, log_dir=tmp_path / "logs", cwd=tmp_path, graceful_stop_sec=1)

    class _DeadProcWithWedgedPipe:
        """An already-exited process whose stdout never signals EOF to a
        blocked reader -- the real-world failure mode this test guards
        against. No reader thread is started for this run, so self._done
        is never set on its own, simulating the wedge directly."""

        def __init__(self) -> None:
            self.returncode = 0
            self.stdout = self
            self.closed = False

        def poll(self):
            return self.returncode

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return self.returncode

        def close(self):
            self.closed = True

    run.proc = _DeadProcWithWedgedPipe()
    run._log_file = open(tmp_path / "fake.log", "w", encoding="utf-8")

    start = time.monotonic()
    run.stop()
    elapsed = time.monotonic() - start

    assert run.poll(), "stop() must recover and finalize even if the reader thread never signals _done"
    assert elapsed < 10, f"stop() took {elapsed:.1f}s to recover -- should be bounded"
    assert run.proc.closed, "stop() should force-close stdout to try to unblock a wedged reader"


def test_suspend_grace_window(tmp_path: Path, monkeypatch) -> None:
    """Regression for Slice R5: cooperative suspend must give a real grace
    window (write control file, wait, THEN SIGTERM) instead of terminating
    in the same tick as the request."""
    from taskqueue.runner import ActiveRun

    db = open_db(tmp_path / "queue.db")
    proj = _project("p", tmp_path)
    path = _yaml(tmp_path, "p", "grace_test", **{
        "command": sys.executable, "args": ["-c", "print('hi')"],
    })
    task = parse_task_yaml(path, proj)
    upsert_task(db, task)
    row = get_task(db, "p", "grace_test")
    run = ActiveRun(conn=db, task_row=row, log_dir=tmp_path / "logs", cwd=tmp_path)

    home = tmp_path
    fake_now = [1000.0]
    monkeypatch.setattr("taskqueue.runner.time.monotonic", lambda: fake_now[0])

    # Before any suspend request: nothing to wait on, grace is trivially "elapsed".
    assert run.suspend_grace_elapsed(10.0) is True

    run.request_cooperative_suspend(home)
    control_file = home / "controls" / "p__grace_test.json"
    assert control_file.is_file(), "control file must be written immediately"
    assert run.suspend_grace_elapsed(10.0) is False, "grace window must not be elapsed right after the request"

    fake_now[0] += 5.0
    assert run.suspend_grace_elapsed(10.0) is False, "still within the grace window"

    fake_now[0] += 6.0  # total 11s since request
    assert run.suspend_grace_elapsed(10.0) is True, "grace window elapsed -> caller may now SIGTERM"

    # Idempotent: a second request doesn't reset the clock.
    fake_now[0] += 1.0
    run.request_cooperative_suspend(home)
    assert run.suspend_requested_at == 1000.0


def test_sync_project_survives_locked_db_on_cleanup(tmp_path: Path) -> None:
    """Regression: a live prod daemon crashed entirely (process exit, every
    running task's monitoring orphaned) when this cleanup DELETE hit
    "database is locked" -- it had no exception handling, unlike the YAML
    parse loop right above it in the same function. A transient SQLite lock
    here must be logged and skipped, never propagate and kill the daemon."""
    from taskqueue.daemon import _sync_project

    db = open_db(tmp_path / "queue.db")
    proj = _project("p", tmp_path, queue_dir=tmp_path / "empty_queue_dir")
    proj.queue_dir.mkdir(parents=True)
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    # A stale task with no matching YAML -- normally deleted by _sync_project's
    # cleanup pass, which is the code path that crashed.
    db.execute(
        "INSERT INTO tasks (project, task_id, status, command, enqueued_at, updated_at, enabled) "
        "VALUES ('p', 'stale', 'queued', 'true', ?, ?, 1)",
        (now, now),
    )
    db.commit()

    class _LockedOnDelete:
        """Proxies a real connection but simulates 'database is locked' on
        the specific DELETE the crash traceback pointed to."""

        def __init__(self, real):
            self._real = real

        def execute(self, sql, *args, **kwargs):
            if sql.strip().upper().startswith("DELETE FROM TASKS"):
                import sqlite3
                raise sqlite3.OperationalError("database is locked")
            return self._real.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._real, name)

    locked_conn = _LockedOnDelete(db)
    _sync_project(locked_conn, proj)  # must not raise

    # The stale row is still there -- cleanup was skipped this cycle, not
    # silently lost -- and will be retried on the next sync pass.
    row = db.execute("SELECT * FROM tasks WHERE project='p' AND task_id='stale'").fetchone()
    assert row is not None


def test_preflight_missing_command(tmp_path: Path) -> None:
    from taskqueue.runner import preflight
    err = preflight(Path("no_such_command_xyz"), ())
    assert err is not None and "not found" in err


def test_preflight_missing_path(tmp_path: Path) -> None:
    from taskqueue.runner import preflight
    err = preflight(Path(sys.executable), (tmp_path / "missing_file.txt",))
    assert err is not None and "missing" in err


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def test_config_roundtrip(tmp_path: Path) -> None:
    from taskqueue.db import get_config, set_config
    db = open_db(tmp_path / "queue.db")
    set_config(db, "cpu_limit_pct", 75)
    cfg = get_config(db)
    assert cfg["cpu_limit_pct"] == 75


# ---------------------------------------------------------------------------
# projects.yaml loading
# ---------------------------------------------------------------------------

def test_load_projects_basic(tmp_path: Path) -> None:
    import yaml
    from taskqueue.config import load_projects
    reg = tmp_path / "projects.yaml"
    reg.write_text(yaml.dump({
        "projects": {
            "pokerbot": {"root": str(tmp_path / "pokerbot")},
            "pimc":     {"root": str(tmp_path / "pimc"), "queue_dir": "queue"},
        }
    }))
    projects = load_projects(tmp_path)
    assert set(projects) == {"pokerbot", "pimc"}
    assert projects["pokerbot"].queue_dir == tmp_path / "pokerbot" / "experiments" / "operator_queue"
    assert projects["pimc"].queue_dir == tmp_path / "pimc" / "queue"


def test_load_projects_missing_file(tmp_path: Path) -> None:
    from taskqueue.config import load_projects
    assert load_projects(tmp_path) == {}
