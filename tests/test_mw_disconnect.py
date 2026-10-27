"""Master–worker disconnect, fencing, and late-finalize rejection."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from taskqueue.db import open_db
from taskqueue._db_runs import reclaim_expired_leases
from taskqueue.worker_api import claim_next_remote_task
from tests.helpers.master_worker import (
    clear_master_lease,
    http_request,
    make_handler_env,
    seed_master_lease,
    seed_queued_task,
    seed_worker,
)


def _claim(db_path, worker_row):
    return claim_next_remote_task(db_path, worker_row, lambda: {"proj": None})


def test_finalize_wrong_token_rejected(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TASKQUEUE_HOME", str(tmp_path))
    HandlerClass, db, _ = make_handler_env(tmp_path)
    seed_queued_task(db, dispatch="remote")
    seed_worker(db, session_id="sess-1")
    claim = _claim(tmp_path / "queue.db", db.execute("SELECT * FROM workers WHERE worker_id='w1'").fetchone())
    run_id = claim["run_id"]
    token = claim["finalize_token"]
    code, data = http_request(
        HandlerClass,
        "POST",
        "/api/worker/finalize",
        {
            "run_id": run_id,
            "worker_id": "w1",
            "session_id": "sess-1",
            "finalize_token": token + "-wrong",
            "status": "completed",
            "exit_code": 0,
        },
    )
    assert code == 200
    assert isinstance(data, dict)
    assert data.get("ok") is False
    assert "finalize_token" in str(data.get("error", ""))
    db.close()


def test_late_finalize_after_reclaim_rejected(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TASKQUEUE_HOME", str(tmp_path))
    HandlerClass, db, _ = make_handler_env(tmp_path)
    seed_queued_task(db, dispatch="remote", execution_semantics="idempotent", idempotent=1)
    seed_worker(db, session_id="sess-1")
    worker = db.execute("SELECT * FROM workers WHERE worker_id='w1'").fetchone()
    claim = _claim(tmp_path / "queue.db", worker)
    run_id = claim["run_id"]
    token = claim["finalize_token"]

    past = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
    db.execute("UPDATE runs SET lease_expires_at = ? WHERE run_id = ?", (past, run_id))
    db.commit()
    reclaimed = reclaim_expired_leases(db)
    assert ("proj", "T1") in reclaimed

    code, data = http_request(
        HandlerClass,
        "POST",
        "/api/worker/finalize",
        {
            "run_id": run_id,
            "worker_id": "w1",
            "session_id": "sess-1",
            "finalize_token": token,
            "status": "completed",
            "exit_code": 0,
        },
    )
    assert isinstance(data, dict)
    assert data.get("ok") is False
    assert "not running" in str(data.get("error", ""))
    db.close()


def test_idempotent_reclaim_requeues_task(tmp_path: Path) -> None:
    db_path = tmp_path / "q.db"
    db = open_db(db_path)
    seed_master_lease(db)
    seed_queued_task(db, dispatch="remote", execution_semantics="idempotent", idempotent=1)
    worker = seed_worker(db, session_id="sess-1")
    claim = _claim(db_path, worker)
    past = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
    db.execute("UPDATE runs SET lease_expires_at = ? WHERE run_id = ?", (past, claim["run_id"]))
    db.commit()
    reclaim_expired_leases(db)
    row = db.execute("SELECT status FROM tasks WHERE task_id='T1'").fetchone()
    assert row["status"] == "queued"
    db.close()


def test_at_most_once_reclaim_goes_unknown(tmp_path: Path) -> None:
    db_path = tmp_path / "q.db"
    db = open_db(db_path)
    seed_master_lease(db)
    seed_queued_task(db, dispatch="remote", execution_semantics="at_most_once")
    worker = seed_worker(db, session_id="sess-1")
    claim = _claim(db_path, worker)
    past = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
    db.execute("UPDATE runs SET lease_expires_at = ? WHERE run_id = ?", (past, claim["run_id"]))
    db.commit()
    reclaim_expired_leases(db)
    row = db.execute("SELECT status FROM tasks WHERE task_id='T1'").fetchone()
    assert row["status"] == "unknown"
    db.close()


def test_longpoll_aborts_when_lease_lost_mid_wait(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TASKQUEUE_HOME", str(tmp_path))
    HandlerClass, db, _ = make_handler_env(tmp_path)
    seed_worker(db, session_id="sess-1")
    clear_master_lease(db)
    t0 = time.monotonic()
    code, data = http_request(
        HandlerClass,
        "GET",
        "/api/worker/next-task?worker_id=w1&session_id=sess-1&protocol=1&wait=2",
    )
    elapsed = time.monotonic() - t0
    assert code == 503
    assert isinstance(data, dict)
    assert data.get("error") == "not master"
    assert elapsed < 1.5
    db.close()
