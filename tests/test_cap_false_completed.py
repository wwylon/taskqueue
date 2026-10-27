"""pokerbot P1: cap/crash must never end `completed`; Row-shaped task_row in the cap path."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import yaml

from taskqueue.config import Project
from taskqueue.daemon import _collect_finished_runs
from taskqueue.db import open_db, parse_task_yaml
from taskqueue._db_tasks import get_task, upsert_task
from taskqueue.runner import ActiveRun


def _db_with_task(tmp_path: Path):
    root = tmp_path
    path = root / "t1.yaml"
    path.write_text(yaml.dump({
        "task_id": "t1", "command": sys.executable, "args": ["-c", "pass"],
        "resources": {"cpu_cores": 1}, "wall_clock_cap_sec": 5,
    }), encoding="utf-8")
    proj = Project(name="p", root=root, queue_dir=root / "q")
    db = open_db(tmp_path / "queue.db")
    upsert_task(db, parse_task_yaml(path, proj))
    return db


def _fake_run(code, stop_reason, crashed=False):
    finalized = {}
    run = types.SimpleNamespace(
        _exit_code=code, stop_reason=stop_reason, run_id=None, reader_crashed=crashed,
        poll=lambda: True, elapsed_sec=lambda: 1.0,
        finalize=lambda status, **kw: finalized.update(status=status, **kw),
    )
    return run, finalized


def _status(db, tmp_path, code, stop_reason, crashed=False):
    run, fin = _fake_run(code, stop_reason, crashed)
    _collect_finished_runs(db, {("p", "t1"): run}, tmp_path)
    return fin["status"]


def test_cap_stop_with_exit0_is_not_completed(tmp_path: Path) -> None:
    db = _db_with_task(tmp_path)
    assert _status(db, tmp_path, 0, "wall_clock_cap") == "interrupted"


def test_crashed_reader_with_exit0_is_failed(tmp_path: Path) -> None:
    db = _db_with_task(tmp_path)
    assert _status(db, tmp_path, 0, None, crashed=True) == "failed"


def test_clean_exit0_still_completed(tmp_path: Path) -> None:
    db = _db_with_task(tmp_path)
    assert _status(db, tmp_path, 0, None) == "completed"


def test_enforce_cap_accepts_sqlite_row(tmp_path: Path) -> None:
    db = _db_with_task(tmp_path)
    row = get_task(db, "p", "t1")
    assert not hasattr(row, "get")  # sqlite3.Row has no .get
    run = object.__new__(ActiveRun)
    run.task_row = row
    run.stop_reason = None
    run.proc = None
    run.db_path = None
    run.graceful_stop_sec = 1
    run._log_file = open(tmp_path / "log.txt", "w", encoding="utf-8")
    run._terminate = lambda: None
    assert run._enforce_wall_clock_cap() is True
    run._log_file.close()
    assert run.stop_reason == "wall_clock_cap"
    assert "wall_clock_cap_sec=5" in (tmp_path / "log.txt").read_text(encoding="utf-8")


def test_pressure_victim_exit75_stays_stopped(tmp_path: Path) -> None:
    from taskqueue.daemon import EXIT_SUSPENDED

    db = _db_with_task(tmp_path)
    assert _status(db, tmp_path, EXIT_SUSPENDED, "ram_hard") == "stopped"
    assert _status(db, tmp_path, EXIT_SUSPENDED, "vram_hard") == "stopped"
    assert _status(db, tmp_path, EXIT_SUSPENDED, None) == "suspended"
