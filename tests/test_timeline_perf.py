"""Timeline forecast: cache, truncation, absolute times, scale."""

from __future__ import annotations

import pytest

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from taskqueue import timeline_forecast as tf
from taskqueue.db import open_db, set_project_limit
from tests.test_operator_features import _insert_run, _mk_task


def _seed(db, n: int, runs: int = 3):
    base = datetime(2026, 9, 1, tzinfo=timezone.utc)
    for i in range(n):
        tid = f"suite_seed{i}"
        _mk_task(db, "proj", tid, priority=i % 5)
        for _ in range(runs):
            _insert_run(db, "proj", f"hist_{i % 50}", base.isoformat(),
                        (base + timedelta(seconds=60 + i % 7)).isoformat())
    db.commit()


def test_bars_have_absolute_times(tmp_path: Path):
    db = open_db(tmp_path / "q.db")
    now = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    _mk_task(db, "proj", "a")
    data = tf.forecast_timeline(db, hours=2, now=now, project="proj")
    b = data["bars"][0]
    assert b["start_at"].startswith("2026-09-20T12:00:00")
    assert b["end_at"] > b["start_at"]
    assert data["bunch_end_at"]


def test_cached_forecast_truncates_and_reports(tmp_path: Path):
    db = open_db(tmp_path / "q.db")
    tf._cache.clear()
    _seed(db, 40, runs=1)
    set_project_limit(db, "proj", "max_tasks", 2)
    d1 = tf.cached_forecast(db, hours=24, project="proj", limit=10)
    assert d1["total_bars"] >= 10 and d1["truncated"] and len(d1["bars"]) == 10
    assert d1["cached"] is False
    d2 = tf.cached_forecast(db, hours=24, project="proj", limit=10)
    assert d2["cached"] is True


def test_cache_invalidated_by_priority_change(tmp_path: Path):
    db = open_db(tmp_path / "q.db")
    tf._cache.clear()
    _mk_task(db, "proj", "a", priority=0)
    _mk_task(db, "proj", "b", priority=0)
    set_project_limit(db, "proj", "max_tasks", 1)
    d1 = tf.cached_forecast(db, hours=6, project="proj")
    first = d1["bars"][0]["task_id"]
    other = "b" if first == "a" else "a"
    db.execute("UPDATE tasks SET priority = 50 WHERE task_id = ?", (other,))
    db.commit()
    d2 = tf.cached_forecast(db, hours=6, project="proj")
    assert d2["cached"] is False
    assert d2["bars"][0]["task_id"] == other


@pytest.mark.slow
def test_large_queue_is_fast(tmp_path: Path):
    db = open_db(tmp_path / "q.db")
    tf._cache.clear()
    _seed(db, 1500, runs=2)
    set_project_limit(db, "proj", "max_tasks", 4)
    t0 = time.monotonic()
    data = tf.cached_forecast(db, hours=24, project="proj", limit=300)
    assert time.monotonic() - t0 < 5.0
    assert data["total_bars"] > 0
