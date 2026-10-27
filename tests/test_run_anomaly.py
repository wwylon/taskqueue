"""run_detail carries an `anomaly` block comparing a run to the task's history."""
from __future__ import annotations

from taskqueue.db import open_db
from taskqueue.operator_features import run_detail


def _run(db, rid, ram, wall=100, status="completed", vram=None):
    db.execute(
        "INSERT INTO runs (run_id, project, task_id, status, started_at, peak_ram_gib, "
        "peak_vram_gib, wall_sec) VALUES (?, 'p', 't', ?, 'n', ?, ?, ?)",
        (rid, status, ram, vram, wall),
    )


def _db(tmp_path):
    return open_db(tmp_path / "q.db")


def test_flags_outlier_ram_and_wall(tmp_path):
    db = _db(tmp_path)
    for i in range(1, 7):
        _run(db, i, ram=4.0, wall=100)
    _run(db, 7, ram=10.0, wall=400)
    db.commit()
    a = run_detail(db, 7)["anomaly"]
    assert a["samples"] == 6
    assert {f["metric"] for f in a["flags"]} == {"ram", "wall"}


def test_normal_run_not_flagged(tmp_path):
    db = _db(tmp_path)
    for i in range(1, 7):
        _run(db, i, ram=4.0)
    _run(db, 7, ram=4.5)
    db.commit()
    assert run_detail(db, 7)["anomaly"]["flags"] == []


def test_too_little_history_not_flagged(tmp_path):
    db = _db(tmp_path)
    for i in range(1, 4):
        _run(db, i, ram=4.0)
    _run(db, 4, ram=40.0)
    db.commit()
    a = run_detail(db, 4)["anomaly"]
    assert a["samples"] == 3 and a["flags"] == []


def test_failed_runs_excluded_from_baseline(tmp_path):
    db = _db(tmp_path)
    for i in range(1, 6):
        _run(db, i, ram=100.0, status="failed")
    _run(db, 6, ram=4.0)
    db.commit()
    assert run_detail(db, 6)["anomaly"]["samples"] == 0
