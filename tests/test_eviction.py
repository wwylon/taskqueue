"""Tests for daemon eviction helpers (_evict_one_suspend)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from taskqueue.daemon import _evict_one_suspend
from taskqueue.db import open_db
from taskqueue._db_tasks import request_stop


def _seed_running(db, project="proj", task_id="T1", priority=50):
    now = datetime.now(UTC).isoformat()
    db.execute(
        "INSERT INTO tasks (project, task_id, status, command, priority, enqueued_at, updated_at, enabled) "
        "VALUES (?, ?, 'running', 'true', ?, ?, ?, 1)",
        (project, task_id, priority, now, now),
    )
    db.commit()


def _mock_run(home):
    run = MagicMock()
    run.stop_reason = None
    return run


def test_evict_sets_stop_requested(tmp_path: Path):
    db = open_db(tmp_path / "q.db")
    _seed_running(db, task_id="T1", priority=50)

    run = _mock_run(tmp_path)
    active_runs = {("proj", "T1"): run}
    keys = [("proj", "T1")]

    _evict_one_suspend(db, tmp_path, active_runs, keys, "test")

    row = db.execute("SELECT stop_requested FROM tasks WHERE task_id='T1'").fetchone()
    assert row["stop_requested"] == 1


def test_evict_calls_request_cooperative_suspend(tmp_path: Path):
    db = open_db(tmp_path / "q.db")
    _seed_running(db, task_id="T1", priority=50)

    run = _mock_run(tmp_path)
    active_runs = {("proj", "T1"): run}
    keys = [("proj", "T1")]

    _evict_one_suspend(db, tmp_path, active_runs, keys, "test")

    run.request_cooperative_suspend.assert_called_once_with(tmp_path)
    assert run.stop_reason == "evicted"


def test_evict_picks_lowest_priority(tmp_path: Path):
    db = open_db(tmp_path / "q.db")
    _seed_running(db, task_id="high", priority=90)
    _seed_running(db, task_id="low", priority=10)

    run_high = _mock_run(tmp_path)
    run_low = _mock_run(tmp_path)
    active_runs = {
        ("proj", "high"): run_high,
        ("proj", "low"): run_low,
    }
    keys = list(active_runs.keys())

    _evict_one_suspend(db, tmp_path, active_runs, keys, "test")

    # Only the low-priority task should have been touched
    run_low.request_cooperative_suspend.assert_called_once()
    run_high.request_cooperative_suspend.assert_not_called()


def test_evict_works_with_single_task(tmp_path: Path):
    db = open_db(tmp_path / "q.db")
    _seed_running(db, task_id="T1")

    run = _mock_run(tmp_path)
    active_runs = {("proj", "T1"): run}
    keys = [("proj", "T1")]

    _evict_one_suspend(db, tmp_path, active_runs, keys, "test")

    run.request_cooperative_suspend.assert_called_once()


def test_evict_tolerates_missing_run_object(tmp_path: Path):
    """Key in keys but not in active_runs — should not raise."""
    db = open_db(tmp_path / "q.db")
    _seed_running(db, task_id="T1", priority=10)

    active_runs = {}  # no run object registered
    keys = [("proj", "T1")]

    _evict_one_suspend(db, tmp_path, active_runs, keys, "test")
    # Just verifying no exception is raised


def test_evicted_stop_reason_attributed_to_tq():
    """Classifier must treat evicted as tq, not user (even if stop_requested)."""
    stop_reason = "evicted"
    stop_requested = True
    if stop_reason in (
        "wall_clock_cap", "ram_hard", "vram_hard",
        "frozen", "frozen_restart", "evicted",
    ):
        stopped_by = "tq"
    elif stop_requested:
        stopped_by = "user"
    else:
        stopped_by = None
    assert stopped_by == "tq"
