"""Timeline forecast: project-average tier, running-task remaining time."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from taskqueue import timeline_forecast as tf
from taskqueue.db import open_db
from tests.test_operator_features import _insert_run, _mk_task

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


def _hist(db, project, tid, secs):
    base = NOW - timedelta(days=2)
    _insert_run(db, project, tid, base.isoformat(), (base + timedelta(seconds=secs)).isoformat())


def test_project_avg_tier_before_global(tmp_path: Path):
    db = open_db(tmp_path / "q.db")
    _hist(db, "p1", "alpha_build", 100)
    _hist(db, "p1", "beta_eval", 300)
    _hist(db, "p2", "zzz_other", 5000)
    idx = tf.build_duration_index(db)
    dur, src = tf.estimate_duration_sec("p1", "totally_new", idx)
    assert src == "project_avg" and abs(dur - 200) < 1
    dur2, src2 = tf.estimate_duration_sec("p3", "x", idx)
    assert src2 == "global_avg"


def _running(db, tid, started, pct=None):
    _mk_task(db, "p", tid, status="running")
    db.execute(
        "INSERT INTO runs (project, task_id, started_at, status, progress_pct) VALUES (?,?,?,?,?)",
        ("p", tid, started.isoformat(), "running", pct),
    )
    db.commit()


def _bar(db, tid):
    data = tf.forecast_timeline(db, hours=4, now=NOW, project="p")
    return next(b for b in data["bars"] if b["task_id"] == tid)


def test_running_overdue_has_floor(tmp_path: Path):
    db = open_db(tmp_path / "q.db")
    _hist(db, "p", "job", 600)
    _running(db, "job", NOW - timedelta(seconds=1200))
    b = _bar(db, "job")
    assert b["estimate_source"] == "overdue"
    assert b["end_sec"] >= 60


def test_running_uses_progress(tmp_path: Path):
    db = open_db(tmp_path / "q.db")
    _hist(db, "p", "job", 600)
    _running(db, "job", NOW - timedelta(seconds=1000), pct=50.0)
    b = _bar(db, "job")
    assert b["estimate_source"] == "progress"
    assert abs(b["end_sec"] - 1000) < 5


def test_running_without_progress_uses_estimate(tmp_path: Path):
    db = open_db(tmp_path / "q.db")
    _hist(db, "p", "job", 600)
    _running(db, "job", NOW - timedelta(seconds=200))
    b = _bar(db, "job")
    assert b["estimate_source"] == "history"
    assert abs(b["end_sec"] - 400) < 5
