"""gpt.md P4: R-005 session fencing, R-011 semantics, R-012 lease fence helpers."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

from taskqueue.config import Project
from taskqueue.db import open_db, parse_task_yaml
from taskqueue._db_runs import reclaim_expired_leases
from taskqueue._db_tasks import retry_task, upsert_task
from taskqueue.fencing import (
    lost_attempt_task_status,
    may_auto_requeue,
    resolve_execution_semantics,
    session_id_mismatch,
    should_stop_for_lost_lease,
)
from tests.helpers.master_worker import http_request, make_handler_env


def test_resolve_execution_semantics() -> None:
    assert resolve_execution_semantics(None, idempotent=False) == "non_idempotent"
    assert resolve_execution_semantics(None, idempotent=True) == "idempotent"
    assert resolve_execution_semantics("at_most_once", idempotent=True) == "at_most_once"
    assert may_auto_requeue("checkpointable")
    assert not may_auto_requeue("at_most_once")
    assert lost_attempt_task_status("idempotent") == "queued"
    assert lost_attempt_task_status("non_idempotent") == "unknown"


def test_should_stop_for_lost_lease() -> None:
    assert should_stop_for_lost_lease(100.0, 200.0, heartbeat_ok=True, margin_sec=15) is False
    assert should_stop_for_lost_lease(100.0, 200.0, heartbeat_ok=False, margin_sec=15) is False
    assert should_stop_for_lost_lease(190.0, 200.0, heartbeat_ok=False, margin_sec=15) is True
    assert should_stop_for_lost_lease(100.0, None, heartbeat_ok=False, margin_sec=15) is False


def test_session_id_mismatch() -> None:
    assert session_id_mismatch(None, "anything") is False
    assert session_id_mismatch("a", "a") is False
    assert session_id_mismatch("a", "b") is True
    assert session_id_mismatch("a", None) is True


def test_old_session_cannot_heartbeat_after_reregister(tmp_path: Path) -> None:
    HandlerClass, db, _ = make_handler_env(tmp_path)

    code, data = http_request(
        HandlerClass,
        "POST",
        "/api/worker/register",
        {
            "worker_id": "w1",
            "session_id": "sess-old",
            "os": "windows",
            "arch": "amd64",
            "ram_gib": 8,
            "cpu_cores": 4,
        },
    )
    assert code == 200
    assert data["session_id"] == "sess-old"
    assert data["worker_epoch"] == 1

    code, data = http_request(
        HandlerClass,
        "POST",
        "/api/worker/register",
        {
            "worker_id": "w1",
            "session_id": "sess-new",
            "os": "windows",
            "arch": "amd64",
            "ram_gib": 8,
            "cpu_cores": 4,
        },
    )
    assert code == 200
    assert data["worker_epoch"] == 2

    code, data = http_request(
        HandlerClass,
        "POST",
        "/api/worker/heartbeat",
        {"worker_id": "w1", "session_id": "sess-old"},
    )
    assert code == 403
    assert data["error"] == "session_id mismatch"

    code, data = http_request(
        HandlerClass,
        "POST",
        "/api/worker/heartbeat",
        {"worker_id": "w1", "session_id": "sess-new"},
    )
    assert code == 200
    assert data.get("ok") is True
    assert "lease_ttl_sec" in data
    db.close()


def test_old_session_cannot_finalize_new_attempt(tmp_path: Path) -> None:
    HandlerClass, db, _ = make_handler_env(tmp_path)
    now = datetime.now(UTC).isoformat()
    db.execute(
        "INSERT INTO workers (worker_id, os, arch, ram_gib, cpu_cores, gpu_vram_gib, "
        "tags, projects, last_seen, status, session_id, worker_epoch) "
        "VALUES ('w1','windows','amd64',8,4,0,'[]','[]',?,'online','sess-new',2)",
        (now,),
    )
    db.execute(
        "INSERT INTO tasks (project, task_id, command, status, enqueued_at, updated_at) "
        "VALUES ('p','t1','python','running',?,?)",
        (now, now),
    )
    db.execute(
        "INSERT INTO runs (project, task_id, started_at, status, log_path, worker_id, "
        "finalize_token, worker_session_id) "
        "VALUES ('p','t1',?,'running',?,'w1','tok','sess-new')",
        (now, str(tmp_path / "r.log")),
    )
    db.commit()
    run_id = db.execute("SELECT run_id FROM runs").fetchone()["run_id"]

    code, data = http_request(
        HandlerClass,
        "POST",
        "/api/worker/finalize",
        {
            "run_id": run_id,
            "worker_id": "w1",
            "session_id": "sess-old",
            "finalize_token": "tok",
            "status": "completed",
            "exit_code": 0,
        },
    )
    assert code == 403
    assert data["error"] == "session_id mismatch"

    code, data = http_request(
        HandlerClass,
        "POST",
        "/api/worker/finalize",
        {
            "run_id": run_id,
            "worker_id": "w1",
            "session_id": "sess-new",
            "finalize_token": "tok",
            "status": "completed",
            "exit_code": 0,
        },
    )
    assert code == 200
    assert data.get("ok") is True
    db.close()


def test_at_most_once_lease_reclaim_unknown(tmp_path: Path) -> None:
    db = open_db(tmp_path / "q.db")
    proj = Project(name="p", root=tmp_path, queue_dir=tmp_path / "q")
    yml = tmp_path / "t.yaml"
    yml.write_text(
        yaml.dump({
            "task_id": "once",
            "command": sys.executable,
            "args": ["-c", "pass"],
            "dispatch": "remote",
            "execution_semantics": "at_most_once",
            "resources": {"cpu_cores": 1},
        }),
        encoding="utf-8",
    )
    task = parse_task_yaml(yml, proj)
    assert task.execution_semantics == "at_most_once"
    assert task.idempotent is False
    upsert_task(db, task)
    past = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
    db.execute(
        "INSERT INTO runs (project, task_id, started_at, status, log_path, worker_id, lease_expires_at) "
        "VALUES ('p','once',?,'running',?,'w1',?)",
        (past, str(tmp_path / "r.log"), past),
    )
    db.execute("UPDATE tasks SET status='running' WHERE project='p' AND task_id='once'")
    db.commit()
    reclaim_expired_leases(db)
    row = db.execute("SELECT status FROM tasks WHERE task_id='once'").fetchone()
    assert row["status"] == "unknown"
    assert retry_task(db, "p", "once") is True
    assert db.execute("SELECT status FROM tasks WHERE task_id='once'").fetchone()["status"] == "queued"
    db.close()


def test_same_session_reregister_does_not_bump_epoch(tmp_path: Path) -> None:
    HandlerClass, db, _ = make_handler_env(tmp_path)
    body = {
        "worker_id": "w1",
        "session_id": "sess-a",
        "os": "linux",
        "arch": "x86_64",
        "ram_gib": 8,
        "cpu_cores": 4,
    }
    _, data1 = http_request(HandlerClass, "POST", "/api/worker/register", body)
    assert data1["worker_epoch"] == 1
    _, data2 = http_request(HandlerClass, "POST", "/api/worker/register", body)
    assert data2["worker_epoch"] == 1
    db.close()
