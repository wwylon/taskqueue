"""Tests for scheduler handoff acceleration and retry safety."""

from __future__ import annotations

import time

from taskqueue._db_tasks import RETRYABLE_STATUSES, retry_task
from taskqueue.db import open_db
from taskqueue.models import STATUS_RUNNING
from taskqueue import sched_wake


def _insert_task(db, task_id: str, *, status: str = "queued") -> None:
    ts = "2020-01-01T00:00:00+00:00"
    db.execute(
        "INSERT INTO tasks (project, task_id, yaml_source, command, args, resources, "
        "status, enabled, priority, exclusive, enqueued_at, updated_at) "
        "VALUES ('p', ?, ?, 'echo', '[]', '{}', ?, 1, 0, 0, ?, ?)",
        (task_id, f"{task_id}.yaml", status, ts, ts),
    )
    db.commit()


def test_sched_wake_returns_early() -> None:
    sched_wake.clear()
    sched_wake.signal_run_finished()
    t0 = time.monotonic()
    assert sched_wake.wait_or_timeout(5.0) is True
    assert time.monotonic() - t0 < 0.5
    sched_wake.clear()


def test_retry_task_does_not_clobber_running(tmp_path) -> None:
    db = open_db(tmp_path / "queue.db")
    _insert_task(db, "t1", status="running")
    assert retry_task(db, "p", "t1") is False
    row = db.execute(
        "SELECT status FROM tasks WHERE project = 'p' AND task_id = 't1'"
    ).fetchone()
    assert row["status"] == STATUS_RUNNING


def test_retry_task_only_from_retryable_statuses(tmp_path) -> None:
    db = open_db(tmp_path / "queue.db")
    _insert_task(db, "t2", status="failed")
    assert retry_task(db, "p", "t2") is True
    row = db.execute(
        "SELECT status FROM tasks WHERE project = 'p' AND task_id = 't2'"
    ).fetchone()
    assert row["status"] == "queued"
    assert "failed" in RETRYABLE_STATUSES


def test_finalize_run_and_task_atomic(tmp_path) -> None:
    from taskqueue.db import finalize_run_and_task

    db = open_db(tmp_path / "queue.db")
    _insert_task(db, "t3", status="running")
    now = "2020-01-01T00:00:00+00:00"
    db.execute(
        "INSERT INTO runs (project, task_id, status, started_at) "
        "VALUES ('p', 't3', 'running', ?)",
        (now,),
    )
    db.commit()
    run_id = int(
        db.execute("SELECT run_id FROM runs WHERE project='p' AND task_id='t3'").fetchone()[
            "run_id"
        ]
    )
    assert finalize_run_and_task(
        db, run_id, "p", "t3", 0, "completed", only_if_running=True
    )
    run_row = db.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()
    task_row = db.execute(
        "SELECT status FROM tasks WHERE project='p' AND task_id='t3'"
    ).fetchone()
    assert run_row["status"] == "completed"
    assert task_row["status"] == "completed"


def test_finalize_run_and_task_rejects_stale_run(tmp_path) -> None:
    from taskqueue.db import finalize_run_and_task

    db = open_db(tmp_path / "queue.db")
    _insert_task(db, "t4", status="running")
    now = "2020-01-01T00:00:00+00:00"
    db.execute(
        "INSERT INTO runs (project, task_id, status, started_at, finished_at) "
        "VALUES ('p', 't4', 'completed', ?, ?)",
        (now, now),
    )
    db.commit()
    run_id = int(
        db.execute("SELECT run_id FROM runs WHERE project='p' AND task_id='t4'").fetchone()[
            "run_id"
        ]
    )
    assert not finalize_run_and_task(
        db, run_id, "p", "t4", 0, "completed", only_if_running=True
    )
    task_row = db.execute(
        "SELECT status FROM tasks WHERE project='p' AND task_id='t4'"
    ).fetchone()
    assert task_row["status"] == STATUS_RUNNING
