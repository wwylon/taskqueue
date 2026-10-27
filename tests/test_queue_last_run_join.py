"""Queue page joins last run via tasks.last_run_id (not correlated MAX)."""

from __future__ import annotations

from pathlib import Path

from taskqueue._db_tasks import bulk_prior_run_seconds, prior_run_seconds
from taskqueue.db import open_db
from taskqueue.web.responses import _get_queue_response, format_server_timing


def test_format_server_timing():
    assert "queue_sql;dur=12" in format_server_timing({"queue_sql": 12.4, "health": 3})


def test_queue_uses_last_run_id_not_max(tmp_path: Path) -> None:
    db = open_db(tmp_path / "q.db")
    now = "2026-01-01T00:00:00+00:00"
    db.execute(
        "INSERT INTO tasks (project, task_id, command, status, enqueued_at, updated_at, enabled) "
        "VALUES (?,?,?,?,?,?,1)",
        ("p", "t1", "python", "failed", now, now),
    )
    # Older run with peaks the UI must show when last_run_id points here.
    c1 = db.execute(
        "INSERT INTO runs (project, task_id, status, started_at, finished_at, peak_ram_gib, progress_pct) "
        "VALUES (?,?,?,?,?,?,?)",
        ("p", "t1", "failed", now, now, 7.5, 42.0),
    )
    old_id = c1.lastrowid
    # Newer run that must NOT be joined when last_run_id stays on old_id.
    db.execute(
        "INSERT INTO runs (project, task_id, status, started_at, finished_at, peak_ram_gib, progress_pct) "
        "VALUES (?,?,?,?,?,?,?)",
        ("p", "t1", "failed", now, now, 1.0, 99.0),
    )
    db.execute(
        "UPDATE tasks SET last_run_id = ? WHERE project = ? AND task_id = ?",
        (old_id, "p", "t1"),
    )
    db.commit()

    out = _get_queue_response(db, project="p", limit=10)
    assert out["total"] == 1
    row = out["tasks"][0]
    assert row["run_id"] == old_id
    assert row["peak_ram_gib"] == 7.5
    assert row["progress_pct"] == 42.0


def test_bulk_prior_matches_scalar_on_queue_path(tmp_path: Path) -> None:
    db = open_db(tmp_path / "q.db")
    now = "2026-01-01T00:00:00+00:00"
    db.execute(
        "INSERT INTO tasks (project, task_id, command, status, enqueued_at, updated_at, enabled) "
        "VALUES (?,?,?,?,?,?,1)",
        ("p", "t", "python", "running", now, now),
    )
    db.execute(
        "INSERT INTO runs (project, task_id, status, started_at, finished_at, wall_sec) "
        "VALUES (?,?,?,?,?,?)",
        ("p", "t", "interrupted", now, now, 100.0),
    )
    db.execute(
        "INSERT INTO runs (project, task_id, status, started_at, finished_at, wall_sec) "
        "VALUES (?,?,?,?,?,?)",
        ("p", "t", "interrupted", now, now, 200.0),
    )
    open_cur = db.execute(
        "INSERT INTO runs (project, task_id, status, started_at) VALUES (?,?,?,?)",
        ("p", "t", "running", now),
    )
    open_id = open_cur.lastrowid
    db.commit()

    scalar = prior_run_seconds(db, "p", "t", open_id)
    bulk = bulk_prior_run_seconds(db, {("p", "t"): {"run_id": open_id}})
    assert bulk[("p", "t")] == scalar
    assert scalar == 300.0
