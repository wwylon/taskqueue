"""W0 emergency-fix regression tests."""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from taskqueue.db import open_db
from taskqueue._db_runs import get_recent_runs, reclaim_expired_leases


# ---------------------------------------------------------------------------
# W0.1: missing indexes
# ---------------------------------------------------------------------------

def test_runs_indexes_exist(tmp_path):
    db = open_db(tmp_path / "q.db")
    idx = {
        row[1]
        for row in db.execute("PRAGMA index_list(runs)").fetchall()
    }
    assert "idx_runs_project_task" in idx, "missing idx_runs_project_task"
    assert "idx_runs_status_worker" in idx, "missing idx_runs_status_worker"


def test_tasks_indexes_exist(tmp_path):
    db = open_db(tmp_path / "q.db")
    idx = {
        row[1]
        for row in db.execute("PRAGMA index_list(tasks)").fetchall()
    }
    assert "idx_tasks_updated_at" in idx, "missing idx_tasks_updated_at"
    assert "idx_tasks_priority_enqueued" in idx, "missing idx_tasks_priority_enqueued"
    assert "idx_tasks_grp" in idx, "missing idx_tasks_grp"
    assert "idx_tasks_project_status" in idx, "missing idx_tasks_project_status"


# ---------------------------------------------------------------------------
# W0.3: history project filter works before LIMIT
# ---------------------------------------------------------------------------

def _seed_runs(db, project: str, count: int) -> None:
    import time
    for i in range(count):
        db.execute(
            "INSERT INTO runs (project, task_id, started_at, status) VALUES (?,?,?,?)",
            (project, f"t{i}", "2026-01-01T00:00:00", "done"),
        )
    db.commit()


def test_history_project_filter_returns_own_rows(tmp_path):
    db = open_db(tmp_path / "q.db")
    # seed 150 rows for 'noisy' and 10 for 'quiet'
    _seed_runs(db, "noisy", 150)
    _seed_runs(db, "quiet", 10)

    rows = get_recent_runs(db, limit=100, project="quiet")
    assert len(rows) == 10
    assert all(r["project"] == "quiet" for r in rows)


def test_history_no_filter_returns_global_limit(tmp_path):
    db = open_db(tmp_path / "q.db")
    _seed_runs(db, "proj", 200)
    rows = get_recent_runs(db, limit=100)
    assert len(rows) == 100


# ---------------------------------------------------------------------------
# W0.4: migration failures are loud
# ---------------------------------------------------------------------------

def test_migration_failure_raises(tmp_path, monkeypatch):
    """open_db must raise, not swallow, a migration exception."""
    import taskqueue.db as tqdb

    original_connect = sqlite3.connect

    class _BadConn:
        """Wraps a real conn but raises on the first ALTER TABLE."""
        def __init__(self, real):
            self._real = real
            self._hit = False

        def __getattr__(self, name):
            return getattr(self._real, name)

        def execute(self, sql, *args, **kwargs):
            if not self._hit and "ALTER TABLE runs" in sql:
                self._hit = True
                raise sqlite3.OperationalError("injected failure")
            return self._real.execute(sql, *args, **kwargs)

        def executescript(self, sql):
            return self._real.executescript(sql)

        def commit(self):
            return self._real.commit()

    def bad_connect(path, *args, **kwargs):
        real = original_connect(path, *args, **kwargs)
        real.row_factory = sqlite3.Row
        return _BadConn(real)

    monkeypatch.setattr(tqdb.sqlite3, "connect", bad_connect)

    with pytest.raises(Exception):
        tqdb.open_db(tmp_path / "q.db")


# ---------------------------------------------------------------------------
# W0.6: active_runs SSE safety (no RuntimeError on concurrent mutation)
# ---------------------------------------------------------------------------

def test_active_runs_list_wrap():
    """Iterating list(d.values()) while d is mutated must not raise RuntimeError."""
    import threading

    d: dict = {}
    errors = []

    class FakeRun:
        def __init__(self, n):
            self.run_id = n

    for i in range(20):
        d[i] = FakeRun(i)

    def mutate():
        for i in range(20, 40):
            d[i] = FakeRun(i)
            if i in d:
                del d[i - 20]

    t = threading.Thread(target=mutate)
    t.start()
    try:
        for _ in range(1000):
            _ = [r.run_id for r in list(d.values())]
    except RuntimeError as e:
        errors.append(e)
    t.join()
    assert not errors, f"RuntimeError during list(dict.values()): {errors}"


# ---------------------------------------------------------------------------
# W0.7: stop_requested cleared on reclaim
# ---------------------------------------------------------------------------

def test_stop_requested_cleared_on_reclaim(tmp_path):
    from datetime import UTC, datetime, timedelta
    import json

    db = open_db(tmp_path / "q.db")

    # Insert a task with stop_requested=1
    db.execute(
        "INSERT INTO tasks (project, task_id, command, status, stop_requested, enqueued_at, updated_at) "
        "VALUES ('p','t1','cmd','running',1,'2026-01-01','2026-01-01')"
    )
    # Insert an expired run
    expired = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
    db.execute(
        "INSERT INTO runs (project, task_id, started_at, status, lease_expires_at, worker_id) "
        "VALUES ('p','t1','2026-01-01T00:00:00','running',?,'w1')",
        (expired,),
    )
    db.commit()

    reclaim_expired_leases(db)

    row = db.execute("SELECT stop_requested, status FROM tasks WHERE task_id='t1'").fetchone()
    assert row["status"] == "unknown", "non-idempotent reclaim → unknown"
    assert row["stop_requested"] == 0, "stop_requested should be cleared after reclaim"
