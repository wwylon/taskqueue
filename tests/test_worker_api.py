"""Tests for taskqueue.worker_api — remote task claim logic."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from taskqueue.db import open_db
from taskqueue.worker_api import claim_next_remote_task


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _insert_task(db, project="proj", task_id="T1", status="queued", dispatch="remote",
                 priority=50, resources=None, depends_on=None, exclusive=False,
                 exclusive_project=False, max_attempts=0, enabled=1):
    now = datetime.now(UTC).isoformat()
    res = json.dumps(resources or {"cpu_cores": 1, "ram_gib": 0.0, "gpu_vram_gib": 0.0})
    deps = json.dumps(depends_on or [])
    db.execute(
        "INSERT INTO tasks (project, task_id, status, command, args, dispatch, priority, "
        "resources, depends_on, exclusive, exclusive_project, max_attempts, enabled, "
        "enqueued_at, updated_at) "
        "VALUES (?, ?, ?, 'true', '[]', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (project, task_id, status, dispatch, priority, res, deps,
         int(exclusive), int(exclusive_project), max_attempts, enabled, now, now),
    )
    db.commit()


def _insert_worker(db, worker_id="w1", projects=None, ram_gib=16.0,
                   cpu_cores=8, gpu_vram_gib=0.0, os="windows", arch="x86_64"):
    now = datetime.now(UTC).isoformat()
    db.execute(
        "INSERT INTO workers (worker_id, os, arch, ram_gib, cpu_cores, gpu_vram_gib, "
        "tags, projects, status, last_seen) "
        "VALUES (?, ?, ?, ?, ?, ?, '[]', ?, 'online', ?)",
        (worker_id, os, arch, ram_gib, cpu_cores, gpu_vram_gib,
         json.dumps(projects or ["proj"]), now),
    )
    db.commit()
    return db.execute(
        "SELECT * FROM workers WHERE worker_id = ?", (worker_id,)
    ).fetchone()


def _claim(db_path, worker_row, projects=None):
    _projects = projects or {"proj": None}
    return claim_next_remote_task(db_path, worker_row, lambda: _projects)


# ---------------------------------------------------------------------------
# Basic claim
# ---------------------------------------------------------------------------

def test_claim_returns_task(tmp_path: Path):
    db_path = tmp_path / "q.db"
    db = open_db(db_path)
    _insert_task(db)
    worker = _insert_worker(db)
    result = _claim(db_path, worker)
    assert result["task"] is not None
    assert result["task"]["task_id"] == "T1"
    assert result["run_id"] is not None
    assert result["finalize_token"] is not None


def test_claim_marks_task_running(tmp_path: Path):
    db_path = tmp_path / "q.db"
    db = open_db(db_path)
    _insert_task(db)
    worker = _insert_worker(db)
    _claim(db_path, worker)
    row = db.execute("SELECT status FROM tasks WHERE task_id='T1'").fetchone()
    assert row["status"] == "running"


def test_claim_nothing_when_empty(tmp_path: Path):
    db_path = tmp_path / "q.db"
    db = open_db(db_path)
    worker = _insert_worker(db)
    result = _claim(db_path, worker)
    assert result["task"] is None


def test_claim_skips_local_only_dispatch(tmp_path: Path):
    db_path = tmp_path / "q.db"
    db = open_db(db_path)
    _insert_task(db, dispatch="local")
    worker = _insert_worker(db)
    result = _claim(db_path, worker)
    assert result["task"] is None


def test_claim_skips_wrong_project(tmp_path: Path):
    db_path = tmp_path / "q.db"
    db = open_db(db_path)
    _insert_task(db, project="other")
    worker = _insert_worker(db, projects=["proj"])  # not "other"
    result = claim_next_remote_task(db_path, worker, lambda: {"proj": None})
    assert result["task"] is None


# ---------------------------------------------------------------------------
# Resource / limit gates
# ---------------------------------------------------------------------------

def test_claim_respects_global_max_running(tmp_path: Path):
    db_path = tmp_path / "q.db"
    db = open_db(db_path)
    # Set a running task to make n_running_total = 1
    _insert_task(db, task_id="T0", status="running")
    _insert_task(db, task_id="T1", status="queued")
    db.execute("INSERT OR REPLACE INTO config (key, value) VALUES ('max_running_tasks', '1')")
    db.commit()
    worker = _insert_worker(db)
    result = _claim(db_path, worker)
    assert result["task"] is None


def test_claim_honours_ram_requirement(tmp_path: Path):
    db_path = tmp_path / "q.db"
    db = open_db(db_path)
    _insert_task(db, resources={"cpu_cores": 1, "ram_gib": 100.0, "gpu_vram_gib": 0.0})
    worker = _insert_worker(db, ram_gib=8.0)  # less than task needs
    result = _claim(db_path, worker)
    assert result["task"] is None


# ---------------------------------------------------------------------------
# Dependency gate
# ---------------------------------------------------------------------------

def test_claim_blocks_on_unmet_dep(tmp_path: Path):
    db_path = tmp_path / "q.db"
    db = open_db(db_path)
    _insert_task(db, task_id="dep", status="queued")
    _insert_task(db, task_id="T1", depends_on=["dep"])
    worker = _insert_worker(db)
    result = _claim(db_path, worker)
    # Should not claim T1 since dep is not completed; dep itself is local dispatch by default
    assert result["task"] is None or result["task"]["task_id"] != "T1"


def test_claim_allows_met_dep(tmp_path: Path):
    db_path = tmp_path / "q.db"
    db = open_db(db_path)
    _insert_task(db, task_id="dep", status="completed")
    _insert_task(db, task_id="T1", depends_on=["dep"])
    worker = _insert_worker(db)
    result = _claim(db_path, worker)
    assert result["task"] is not None
    assert result["task"]["task_id"] == "T1"


# ---------------------------------------------------------------------------
# Exclusive lock
# ---------------------------------------------------------------------------

def test_claim_blocks_exclusive_when_running(tmp_path: Path):
    db_path = tmp_path / "q.db"
    db = open_db(db_path)
    _insert_task(db, task_id="T0", status="running")
    _insert_task(db, task_id="T1", exclusive=True)
    worker = _insert_worker(db)
    result = _claim(db_path, worker)
    assert result["task"] is None


def test_claim_blocks_any_when_exclusive_running(tmp_path: Path):
    db_path = tmp_path / "q.db"
    db = open_db(db_path)
    _insert_task(db, task_id="T0", status="running", exclusive=True)
    _insert_task(db, task_id="T1")
    worker = _insert_worker(db)
    result = _claim(db_path, worker)
    assert result["task"] is None


def test_claim_blocks_exclusive_project_when_same_project_running(tmp_path: Path):
    db_path = tmp_path / "q.db"
    db = open_db(db_path)
    _insert_task(db, task_id="T0", status="running")
    _insert_task(db, task_id="T1", exclusive_project=True)
    worker = _insert_worker(db)
    result = _claim(db_path, worker)
    assert result["task"] is None


def test_claim_blocks_same_project_when_exclusive_project_running(tmp_path: Path):
    db_path = tmp_path / "q.db"
    db = open_db(db_path)
    _insert_task(db, task_id="T0", status="running", exclusive_project=True)
    _insert_task(db, task_id="T1")
    worker = _insert_worker(db)
    result = _claim(db_path, worker)
    assert result["task"] is None


def test_claim_allows_other_project_when_exclusive_project_running(tmp_path: Path):
    db_path = tmp_path / "q.db"
    db = open_db(db_path)
    _insert_task(db, project="proj", task_id="T0", status="running", exclusive_project=True)
    _insert_task(db, project="other", task_id="T1")
    worker = _insert_worker(db, projects=["proj", "other"])
    result = _claim(db_path, worker)
    assert result["task"] is not None
    assert result["task"]["project"] == "other"
    assert result["task"]["task_id"] == "T1"


# ---------------------------------------------------------------------------
# use_resume flag
# ---------------------------------------------------------------------------

def test_claim_sets_use_resume_for_interrupted(tmp_path: Path):
    db_path = tmp_path / "q.db"
    db = open_db(db_path)
    _insert_task(db)
    now = datetime.now(UTC).isoformat()
    db.execute(
        "INSERT INTO runs (project, task_id, started_at, status, log_path) "
        "VALUES ('proj', 'T1', ?, 'interrupted', 'x.log')",
        (now,),
    )
    db.commit()
    worker = _insert_worker(db)
    result = _claim(db_path, worker)
    assert result.get("use_resume") is True


def test_claim_no_resume_for_fresh_task(tmp_path: Path):
    db_path = tmp_path / "q.db"
    db = open_db(db_path)
    _insert_task(db)
    worker = _insert_worker(db)
    result = _claim(db_path, worker)
    assert result.get("use_resume") is False
