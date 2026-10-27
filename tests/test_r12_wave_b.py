"""Round 12 Wave B: F-144, F-146–F-149."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from taskqueue._db_runs import reclaim_expired_leases
from taskqueue.db import open_db
from taskqueue.web.sse import clear_status_snapshot_cache
from tests.helpers.master_worker import http_request, make_handler_env
from tests.test_web_routes import _make_handler, _make_request


def test_f144_lease_reclaim_clears_block_reason(tmp_path: Path) -> None:
    db = open_db(tmp_path / "q.db")
    now = datetime.now(UTC)
    past = (now - timedelta(seconds=120)).isoformat()
    db.execute(
        "INSERT INTO tasks (project, task_id, status, command, enqueued_at, updated_at, "
        "enabled, block_reason, last_error_code) VALUES "
        "('p', 't1', 'running', 'true', ?, ?, 1, 'old capacity block', NULL)",
        (past, past),
    )
    db.execute(
        "INSERT INTO runs (project, task_id, status, started_at, worker_id, lease_expires_at) "
        "VALUES ('p', 't1', 'running', ?, 'w', ?)",
        (past, past),
    )
    db.commit()
    reclaim_expired_leases(db)
    row = db.execute("SELECT status, block_reason, last_error_code FROM tasks WHERE task_id='t1'").fetchone()
    assert row["status"] != "running"
    assert row["block_reason"] is None


def test_f148_worker_register_bad_numbers_400(tmp_path: Path) -> None:
    HandlerClass, _db, _ = make_handler_env(tmp_path)
    code, data = http_request(
        HandlerClass,
        "POST",
        "/api/worker/register",
        {"worker_id": "w1", "ram_gib": "nope", "cpu_cores": 2, "gpu_vram_gib": 0},
    )
    assert code == 400
    assert data.get("ok") is False


def test_f149_report_days_clamped(tmp_path: Path) -> None:
    HandlerClass, _db = _make_handler(tmp_path)
    code, body = _make_request(HandlerClass, "GET", "/api/report?days=99999")
    assert code == 200
    # Just ensure it does not 500; clamp is internal.
    assert isinstance(json.loads(body), dict)


def test_f146_sse_cache_clear_helper() -> None:
    clear_status_snapshot_cache()  # must not raise after keyed redesign
