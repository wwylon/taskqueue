"""Daemon _detect_hung_remote for online workers with stale progress."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from taskqueue.daemon import _detect_hung_remote
from taskqueue.db import open_db
from tests.helpers.master_worker import seed_master_lease, seed_queued_task, seed_worker


def _seed_running_remote(db, *, semantics: str = "idempotent", idempotent: int = 1) -> int:
    seed_queued_task(db, dispatch="remote", execution_semantics=semantics, idempotent=idempotent)
    now = datetime.now(UTC).isoformat()
    db.execute(
        "UPDATE tasks SET status='running' WHERE task_id='T1'"
    )
    db.execute(
        "INSERT INTO runs (project, task_id, status, started_at, worker_id, "
        "last_log_line, progress_pct, lease_expires_at) "
        "VALUES ('proj', 'T1', 'running', ?, 'w1', 'stale', 0.0, ?)",
        (now, now),
    )
    db.commit()
    return db.execute("SELECT run_id FROM runs").fetchone()["run_id"]


def test_hung_idempotent_requeues(tmp_path: Path) -> None:
    db = open_db(tmp_path / "q.db")
    seed_master_lease(db)
    seed_worker(db)
    run_id = _seed_running_remote(db, semantics="idempotent", idempotent=1)
    hung_state: dict = {}
    cfg = {"hung_task_k": 3}
    for _ in range(5):
        _detect_hung_remote(db, cfg, hung_state)
    row = db.execute("SELECT status FROM tasks WHERE task_id='T1'").fetchone()
    assert row["status"] == "queued"
    run = db.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()
    assert run["status"] == "interrupted"
    # Heartbeat fences the old worker because run.status != 'running'
    # (even though stop_requested was cleared for the new queued attempt).
    db.close()


def test_hung_at_most_once_escalates_to_unknown(tmp_path: Path) -> None:
    db = open_db(tmp_path / "q.db")
    seed_master_lease(db)
    seed_worker(db)
    run_id = _seed_running_remote(db, semantics="at_most_once", idempotent=0)
    hung_state: dict = {}
    cfg = {"hung_task_k": 2}
    for _ in range(8):
        _detect_hung_remote(db, cfg, hung_state)
    row = db.execute("SELECT status FROM tasks WHERE task_id='T1'").fetchone()
    assert row["status"] == "unknown"
    run = db.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()
    assert run["status"] == "failed"
    db.close()


def test_hung_requires_wall_clock_when_tick_sec_given(tmp_path: Path) -> None:
    db = open_db(tmp_path / "q.db")
    seed_master_lease(db)
    seed_worker(db)
    _seed_running_remote(db, semantics="idempotent", idempotent=1)
    hung_state: dict = {}
    for _ in range(10):  # ten rapid passes, but 1 tick = 60 s of wall-clock
        _detect_hung_remote(db, {"hung_task_k": 3}, hung_state, tick_sec=60.0)
    row = db.execute("SELECT status FROM tasks WHERE task_id='T1'").fetchone()
    assert row["status"] == "running"
    db.close()
