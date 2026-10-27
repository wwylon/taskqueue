"""Excess-stop ordering must read run ids from ``runs`` (``tasks`` rows have no ``run_id`` column).

Regression: ``r["run_id"]`` on a tasks row raised ``IndexError: No item with that key`` on every
admit tick while more tasks ran than max_running_tasks / project max_tasks allowed (118 tracebacks in
the 2026-10-02 daemon log), so the limit was never enforced and the whole scheduler tick failed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from taskqueue._db_tasks import get_running_tasks
from taskqueue.db import open_db
from taskqueue.scheduler import _most_recent_first


def _task(db, project, task_id, status="running"):
    now = datetime.now(UTC).isoformat()
    db.execute(
        "INSERT INTO tasks (project, task_id, status, command, enqueued_at, updated_at, enabled, grp) "
        "VALUES (?, ?, ?, 'true', ?, ?, 1, '')",
        (project, task_id, status, now, now),
    )


def _run(db, project, task_id, status="running"):
    now = datetime.now(UTC).isoformat()
    db.execute(
        "INSERT INTO runs (project, task_id, started_at, status) VALUES (?, ?, ?, ?)",
        (project, task_id, now, status),
    )


def test_tasks_rows_have_no_run_id_column(tmp_path: Path):
    db = open_db(tmp_path / "q.db")
    _task(db, "p", "a")
    row = get_running_tasks(db)[0]
    assert "run_id" not in row.keys()  # the premise of the old bug


def test_most_recent_run_first_and_no_indexerror(tmp_path: Path):
    db = open_db(tmp_path / "q.db")
    for t in ("old", "mid", "new", "norun"):
        _task(db, "p", t)
    _run(db, "p", "old")
    _run(db, "p", "mid")
    _run(db, "p", "new")  # highest run_id = most recently started
    db.commit()
    ordered = [r["task_id"] for r in _most_recent_first(db, get_running_tasks(db))]
    assert ordered[:3] == ["new", "mid", "old"]
    assert ordered[-1] == "norun"  # no run row sorts last (treated as run_id 0)


def test_uses_latest_running_run_per_task_and_ignores_finished_runs(tmp_path: Path):
    db = open_db(tmp_path / "q.db")
    _task(db, "p", "a")
    _task(db, "p", "b")
    _run(db, "p", "a")                         # run 1 (running)
    _run(db, "p", "b")                         # run 2 (running)
    _run(db, "p", "a", status="completed")     # run 3 finished: must not make "a" look newer
    db.commit()
    ordered = [r["task_id"] for r in _most_recent_first(db, get_running_tasks(db))]
    assert ordered == ["b", "a"]


def test_empty_input(tmp_path: Path):
    db = open_db(tmp_path / "q.db")
    assert _most_recent_first(db, []) == []
