"""Master–worker dispatch matrix and scheduler routing gates."""

from __future__ import annotations

from pathlib import Path

import pytest

from taskqueue.config import Project
from taskqueue.scheduler import MASTER_NODE_ID, FilterContext, Node, filter_task
from taskqueue.worker_api import claim_next_remote_task
from tests.helpers.master_worker import (
    http_request,
    local_dispatch_allowed,
    make_handler_env,
    seed_master_lease,
    seed_queued_task,
    seed_worker,
)


def _claim(db_path, worker_row, projects=None):
    projs = projects or {"proj": None}
    return claim_next_remote_task(db_path, worker_row, lambda: projs)


def test_local_dispatch_not_claimable_by_worker(tmp_path: Path) -> None:
    db_path = tmp_path / "q.db"
    from taskqueue.db import open_db

    db = open_db(db_path)
    seed_master_lease(db)
    seed_queued_task(db, dispatch="local")
    worker = seed_worker(db)
    result = _claim(db_path, worker)
    assert result.get("task") is None


def test_remote_dispatch_claimed_by_worker(tmp_path: Path) -> None:
    db_path = tmp_path / "q.db"
    from taskqueue.db import open_db

    db = open_db(db_path)
    seed_master_lease(db)
    seed_queued_task(db, dispatch="remote")
    worker = seed_worker(db)
    result = _claim(db_path, worker)
    assert result.get("task") is not None
    assert result["task"]["task_id"] == "T1"


def test_remote_blocked_on_master_when_workers_online(tmp_path: Path) -> None:
    db_path = tmp_path / "q.db"
    from taskqueue.db import open_db

    db = open_db(db_path)
    seed_master_lease(db)
    seed_worker(db)
    assert local_dispatch_allowed(db, "remote") is False


def test_remote_fallback_on_master_when_no_workers(tmp_path: Path) -> None:
    db_path = tmp_path / "q.db"
    from taskqueue.db import open_db

    db = open_db(db_path)
    seed_master_lease(db)
    assert local_dispatch_allowed(db, "remote") is True


def test_any_dispatch_claimable_by_worker(tmp_path: Path) -> None:
    db_path = tmp_path / "q.db"
    from taskqueue.db import open_db

    db = open_db(db_path)
    seed_master_lease(db)
    seed_queued_task(db, dispatch="any", task_id="ANY1")
    worker = seed_worker(db)
    result = _claim(db_path, worker)
    assert result.get("task") is not None


def test_draining_worker_cannot_claim(tmp_path: Path) -> None:
    db_path = tmp_path / "q.db"
    from taskqueue.db import open_db

    db = open_db(db_path)
    seed_master_lease(db)
    seed_queued_task(db, dispatch="remote")
    worker = seed_worker(db, status="draining")
    result = _claim(db_path, worker)
    assert result.get("task") is None


def test_worker_missing_project_skips_task(tmp_path: Path) -> None:
    db_path = tmp_path / "q.db"
    from taskqueue.db import open_db

    db = open_db(db_path)
    seed_master_lease(db)
    seed_queued_task(db, project="other", task_id="X1", dispatch="remote")
    worker = seed_worker(db, projects=["proj"])
    result = _claim(db_path, worker)
    assert result.get("task") is None


def test_requires_os_mismatch_blocks_claim(tmp_path: Path) -> None:
    db_path = tmp_path / "q.db"
    from taskqueue.db import open_db

    db = open_db(db_path)
    seed_master_lease(db)
    seed_queued_task(db, dispatch="remote", requires='{"os": "linux"}')
    worker = seed_worker(db, os="windows")
    result = _claim(db_path, worker)
    assert result.get("task") is None


def test_local_master_dispatch_filter_with_online_workers() -> None:
    row = {
        "project": "proj",
        "task_id": "T1",
        "dispatch": "remote",
        "exclusive": 0,
        "resources": "{}",
        "requires": "{}",
        "depends_on": "[]",
        "max_attempts": 0,
        "force_run": 0,
        "requires_services": "[]",
    }
    local = Node(node_id=MASTER_NODE_ID, is_local=True, os="windows", arch="x86_64")
    ctx = FilterContext(online_workers=1, dep_status=lambda p, t: None)
    fr = filter_task(row, local, ctx, cpu=1.0, ram=1.0, gpu=0.0)
    assert fr.ok is False


def test_next_task_http_returns_task(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TASKQUEUE_HOME", str(tmp_path))
    HandlerClass, db, home = make_handler_env(tmp_path)
    seed_queued_task(db, dispatch="remote")
    seed_worker(db, session_id="sess-1")
    code, data = http_request(
        HandlerClass,
        "GET",
        "/api/worker/next-task?worker_id=w1&session_id=sess-1&protocol=1&wait=0",
    )
    assert code == 200
    assert isinstance(data, dict)
    assert data.get("task") is not None
    db.close()


def test_claim_whose_response_write_fails_is_reclaimable_immediately(tmp_path: Path, monkeypatch) -> None:
    from taskqueue.web.handler import Handler  # noqa: F401  (ensures _json exists)

    monkeypatch.setenv("TASKQUEUE_HOME", str(tmp_path))
    HandlerClass, db, home = make_handler_env(tmp_path)
    seed_queued_task(db, dispatch="remote")
    seed_worker(db, session_id="sess-1")

    def broken(self, data, status=200):
        raise BrokenPipeError("client went away")

    monkeypatch.setattr(HandlerClass, "_json", broken)
    try:
        http_request(HandlerClass, "GET", "/api/worker/next-task?worker_id=w1&session_id=sess-1&protocol=1&wait=0")
    except Exception:
        pass
    row = db.execute("SELECT lease_expires_at FROM runs WHERE status = 'running'").fetchone()
    from taskqueue.db import lease_expiry_iso

    assert row is not None and row["lease_expires_at"] < lease_expiry_iso(0)
    db.close()


def test_finalize_run_and_task_applies_task_error_fields_in_one_commit(tmp_path: Path, monkeypatch) -> None:
    from taskqueue.db import finalize_run_and_task

    monkeypatch.setenv("TASKQUEUE_HOME", str(tmp_path))
    _, db, _ = make_handler_env(tmp_path)
    tid = seed_queued_task(db, dispatch="remote")
    row = db.execute("SELECT project, task_id FROM tasks LIMIT 1").fetchone()
    proj, tid = row["project"], row["task_id"]
    db.execute("INSERT INTO runs (project, task_id, status, started_at, log_path) VALUES (?, ?, 'running', '2026-01-01T00:00:00+00:00', 'l')", (proj, tid))
    db.execute("UPDATE tasks SET status='running', last_error_code='OLD', block_reason='x', cap_auto_retry_count=3 WHERE project=? AND task_id=?", (proj, tid))
    db.commit()
    run_id = db.execute("SELECT run_id FROM runs").fetchone()["run_id"]
    assert finalize_run_and_task(db, run_id, proj, tid, 0, "completed", clear_task_error=True)
    t = db.execute("SELECT status, last_error_code, block_reason, cap_auto_retry_count FROM tasks WHERE project=? AND task_id=?", (proj, tid)).fetchone()
    assert (t["status"], t["last_error_code"], t["block_reason"], t["cap_auto_retry_count"]) == ("completed", None, None, 0)
    db.close()
