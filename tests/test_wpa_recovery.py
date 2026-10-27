"""WP-A: local-only orphan requeue, exclusive tick, start compensation, leases."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import yaml

from taskqueue import errors as tqerr
from taskqueue.config import Project
from taskqueue.daemon import requeue_orphaned_running
from taskqueue._db_tasks import claim_task_running, get_task, set_task_status, upsert_task
from taskqueue._db_runs import insert_run, reclaim_expired_leases, renew_run_lease
from taskqueue.db import open_db, parse_task_yaml
from taskqueue.models import STATUS_QUEUED, STATUS_RUNNING
from taskqueue.runner import ActiveRun


def _project(root: Path) -> Project:
    return Project(name="p", root=root, queue_dir=root / "q")


def _upsert(db, root: Path, task_id: str, **extra) -> None:
    yml = root / f"{task_id}.yaml"
    data = {
        "task_id": task_id,
        "command": sys.executable,
        "args": ["-c", "pass"],
        "resources": {"cpu_cores": 1},
        "enabled": True,
        **extra,
    }
    yml.write_text(yaml.dump(data), encoding="utf-8")
    upsert_task(db, parse_task_yaml(yml, _project(root)))


def test_requeue_spares_remote_running(tmp_path: Path) -> None:
    db = open_db(tmp_path / "queue.db")
    _upsert(db, tmp_path, "local")
    _upsert(db, tmp_path, "remote", dispatch="remote")
    set_task_status(db, "p", "local", STATUS_RUNNING)
    set_task_status(db, "p", "remote", STATUS_RUNNING)
    insert_run(db, "p", "local", str(tmp_path / "local.log"))  # worker_id NULL
    past = datetime.now(UTC).isoformat()
    db.execute(
        "INSERT INTO runs (project, task_id, started_at, status, log_path, worker_id, lease_expires_at) "
        "VALUES ('p', 'remote', ?, 'running', ?, 'w1', ?)",
        (past, str(tmp_path / "r.log"), past),
    )
    db.commit()

    n = requeue_orphaned_running(db, reason="clock-jump")
    assert n == 1
    assert get_task(db, "p", "local")["status"] == STATUS_QUEUED
    assert get_task(db, "p", "remote")["status"] == STATUS_RUNNING
    remote_run = db.execute(
        "SELECT status FROM runs WHERE task_id = 'remote' ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    assert remote_run["status"] == "running"


def test_claim_rejects_disabled(tmp_path: Path) -> None:
    db = open_db(tmp_path / "queue.db")
    _upsert(db, tmp_path, "off", enabled=False)
    db.execute("UPDATE tasks SET enabled = 0, status = 'queued' WHERE task_id = 'off'")
    db.commit()
    assert claim_task_running(db, "p", "off") is False
    assert get_task(db, "p", "off")["status"] == STATUS_QUEUED


def test_start_compensates_when_popen_fails(tmp_path: Path) -> None:
    db = open_db(tmp_path / "queue.db")
    _upsert(db, tmp_path, "spawn")
    row = get_task(db, "p", "spawn")
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    run = ActiveRun(
        conn=db,
        task_row=row,
        log_dir=log_dir,
        cwd=tmp_path,
        graceful_stop_sec=5,
        db_path=tmp_path / "queue.db",
    )
    with patch("taskqueue.runner.subprocess.Popen", side_effect=OSError("boom")):
        err = run.start(use_resume=False)
    assert err is not None
    assert "start failed" in err
    assert get_task(db, "p", "spawn")["status"] == "failed"
    assert get_task(db, "p", "spawn")["last_error_code"] == tqerr.RUN_PREFLIGHT
    last = db.execute(
        "SELECT status FROM runs WHERE task_id = 'spawn' ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    assert last["status"] == "failed"


def test_reclaim_skips_renewed_lease(tmp_path: Path) -> None:
    db = open_db(tmp_path / "q.db")
    _upsert(db, tmp_path, "remote1", dispatch="remote")
    past = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
    cur = db.execute(
        "INSERT INTO runs (project, task_id, started_at, status, log_path, worker_id, lease_expires_at) "
        "VALUES ('p', 'remote1', ?, 'running', ?, 'w1', ?)",
        (past, str(tmp_path / "r.log"), past),
    )
    run_id = cur.lastrowid
    db.execute("UPDATE tasks SET status = 'running' WHERE task_id = 'remote1'")
    db.commit()
    renew_run_lease(db, run_id, ttl_sec=120)
    assert reclaim_expired_leases(db) == []
    assert get_task(db, "p", "remote1")["status"] == STATUS_RUNNING


def test_exclusive_blocks_after_n_running_increment() -> None:
    """Mirror schedule-loop exclusive gate after a successful non-exclusive start."""
    n_running = 0
    has_exclusive = False
    started: list[str] = []
    queued = [
        {"task_id": "a", "exclusive": 0},
        {"task_id": "ex", "exclusive": 1},
    ]
    for row in queued:
        exclusive = bool(row["exclusive"])
        if exclusive and n_running > 0:
            continue
        if has_exclusive:
            continue
        started.append(row["task_id"])
        n_running += 1
        if exclusive:
            has_exclusive = True
            break
    assert started == ["a"]
    assert n_running == 1
