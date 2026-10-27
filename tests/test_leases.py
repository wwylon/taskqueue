"""Lease TTL reclaim and worker offline demotion."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

from taskqueue import errors as tqerr
from taskqueue.config import Project
from taskqueue.db import SCHEMA_VERSION, open_db, parse_task_yaml
from taskqueue._db_tasks import get_task, upsert_task
from taskqueue._db_runs import (
    mark_stale_workers_offline,
    reclaim_expired_leases,
    renew_run_lease,
    worker_health,
)


def test_schema_has_lease_column(tmp_path: Path) -> None:
    assert SCHEMA_VERSION >= 4
    db = open_db(tmp_path / "q.db")
    cols = {r["name"] for r in db.execute("PRAGMA table_info(runs)").fetchall()}
    assert "lease_expires_at" in cols
    task_cols = {r["name"] for r in db.execute("PRAGMA table_info(tasks)").fetchall()}
    assert "started_by" in task_cols


def test_reclaim_expired_lease(tmp_path: Path) -> None:
    db = open_db(tmp_path / "q.db")
    proj = Project(name="p", root=tmp_path, queue_dir=tmp_path / "q")
    yml = tmp_path / "t.yaml"
    yml.write_text(
        yaml.dump({
            "task_id": "remote1",
            "command": sys.executable,
            "args": ["-c", "pass"],
            "dispatch": "remote",
            "resources": {"cpu_cores": 1},
        }),
        encoding="utf-8",
    )
    upsert_task(db, parse_task_yaml(yml, proj))
    past = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
    db.execute(
        "INSERT INTO runs (project, task_id, started_at, status, log_path, worker_id, lease_expires_at) "
        "VALUES (?, ?, ?, 'running', ?, ?, ?)",
        ("p", "remote1", past, str(tmp_path / "r.log"), "w1", past),
    )
    db.execute(
        "UPDATE tasks SET status = 'running' WHERE project = ? AND task_id = ?",
        ("p", "remote1"),
    )
    db.commit()

    got = reclaim_expired_leases(db)
    assert got == [("p", "remote1")]
    # R-011: default non_idempotent → unknown (not auto-requeued)
    assert get_task(db, "p", "remote1")["status"] == "unknown"
    assert get_task(db, "p", "remote1")["last_error_code"] == tqerr.INFRA_WORKER_LOST
    run = db.execute(
        "SELECT status, last_error_code, wall_sec, error_category FROM runs ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    assert run["status"] == "interrupted"
    assert run["last_error_code"] == tqerr.INFRA_WORKER_LOST
    assert run["wall_sec"] is not None and run["wall_sec"] >= 119
    assert run["error_category"] is not None


def test_reclaim_idempotent_lease_requeues(tmp_path: Path) -> None:
    db = open_db(tmp_path / "q.db")
    proj = Project(name="p", root=tmp_path, queue_dir=tmp_path / "q")
    yml = tmp_path / "t.yaml"
    yml.write_text(
        yaml.dump({
            "task_id": "remote1",
            "command": sys.executable,
            "args": ["-c", "pass"],
            "dispatch": "remote",
            "idempotent": True,
            "resources": {"cpu_cores": 1},
        }),
        encoding="utf-8",
    )
    upsert_task(db, parse_task_yaml(yml, proj))
    past = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
    db.execute(
        "INSERT INTO runs (project, task_id, started_at, status, log_path, worker_id, lease_expires_at) "
        "VALUES (?, ?, ?, 'running', ?, ?, ?)",
        ("p", "remote1", past, str(tmp_path / "r.log"), "w1", past),
    )
    db.execute(
        "UPDATE tasks SET status = 'running' WHERE project = ? AND task_id = ?",
        ("p", "remote1"),
    )
    db.commit()

    got = reclaim_expired_leases(db)
    assert got == [("p", "remote1")]
    assert get_task(db, "p", "remote1")["status"] == "queued"


def test_worker_health_three_states() -> None:
    now = datetime.now(UTC)
    fresh = now.isoformat()
    missed_a_beat = (now - timedelta(seconds=90)).isoformat()  # > 2x30s, < 180s offline_sec
    long_gone = (now - timedelta(seconds=300)).isoformat()  # > 180s offline_sec
    assert worker_health(fresh, heartbeat_sec=30) == "online"
    assert worker_health(missed_a_beat, heartbeat_sec=30, offline_sec=180) == "unreachable"
    assert worker_health(long_gone, heartbeat_sec=30, offline_sec=180) == "offline"
    assert worker_health(None) == "offline"


def test_reclaim_distinguishes_worker_alive_vs_lost(tmp_path: Path) -> None:
    """Regression for Slice R7: lease reclaim used to always tag
    tq.infra.worker_lost regardless of whether the worker was actually
    dead. A stuck task on a healthy (still-heartbeating) worker must be
    tagged distinctly so an operator/agent doesn't chase a dead-worker
    investigation when the worker itself is fine."""
    db = open_db(tmp_path / "q.db")
    proj = Project(name="p", root=tmp_path, queue_dir=tmp_path / "q")
    yml = tmp_path / "t.yaml"
    yml.write_text(
        yaml.dump({
            "task_id": "remote1",
            "command": sys.executable,
            "args": ["-c", "pass"],
            "dispatch": "remote",
            "resources": {"cpu_cores": 1},
        }),
        encoding="utf-8",
    )
    upsert_task(db, parse_task_yaml(yml, proj))
    past = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
    fresh = datetime.now(UTC).isoformat()
    db.execute(
        "INSERT INTO runs (project, task_id, started_at, status, log_path, worker_id, lease_expires_at) "
        "VALUES (?, ?, ?, 'running', ?, ?, ?)",
        ("p", "remote1", past, str(tmp_path / "r.log"), "w1", past),
    )
    db.execute(
        "UPDATE tasks SET status = 'running' WHERE project = ? AND task_id = ?",
        ("p", "remote1"),
    )
    # Worker w1 is still heartbeating right now -- lease expiry here means a
    # stuck task, not a dead worker.
    db.execute(
        "INSERT INTO workers (worker_id, os, arch, ram_gib, cpu_cores, gpu_vram_gib, tags, projects, last_seen, status) "
        "VALUES ('w1', 'linux', 'x86_64', 8, 4, 0, '[]', '[]', ?, 'online')",
        (fresh,),
    )
    db.commit()

    got = reclaim_expired_leases(db, heartbeat_sec=30)
    assert got == [("p", "remote1")]
    assert get_task(db, "p", "remote1")["last_error_code"] == tqerr.INFRA_LEASE_EXPIRED_WORKER_ALIVE


def test_renew_lease_extends(tmp_path: Path) -> None:
    db = open_db(tmp_path / "q.db")
    past = (datetime.now(UTC) - timedelta(seconds=10)).isoformat()
    cur = db.execute(
        "INSERT INTO runs (project, task_id, started_at, status, log_path, worker_id, lease_expires_at) "
        "VALUES ('p','t', ?, 'running', 'x.log', 'w1', ?)",
        (past, past),
    )
    run_id = cur.lastrowid
    db.commit()
    renew_run_lease(db, run_id, ttl_sec=90)
    row = db.execute("SELECT lease_expires_at FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    assert row["lease_expires_at"] > past


def test_mark_stale_workers_offline(tmp_path: Path) -> None:
    db = open_db(tmp_path / "q.db")
    old = (datetime.now(UTC) - timedelta(seconds=600)).isoformat()
    db.execute(
        "INSERT INTO workers (worker_id, os, arch, ram_gib, cpu_cores, gpu_vram_gib, tags, projects, last_seen, status) "
        "VALUES ('dead', 'linux', 'x86_64', 8, 4, 0, '[]', '[]', ?, 'online')",
        (old,),
    )
    db.commit()
    assert mark_stale_workers_offline(db, offline_sec=180) == 1
    st = db.execute("SELECT status FROM workers WHERE worker_id = 'dead'").fetchone()["status"]
    assert st == "offline"
