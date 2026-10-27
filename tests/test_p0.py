"""Slice A P0 correctness tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

from taskqueue.config import Project
from taskqueue.daemon import clock_jump_threshold, is_clock_jump, requeue_orphaned_running
from taskqueue.scheduler import should_schedule_locally
from taskqueue._db_tasks import (
    claim_task_running,
    enable_task,
    get_queued_tasks,
    get_task,
    set_task_status,
    upsert_task,
)
from taskqueue._db_runs import force_task, insert_run
from taskqueue.db import open_db, parse_task_yaml
from taskqueue.models import STATUS_DISABLED, STATUS_QUEUED, STATUS_RUNNING


def _project(name: str, root: Path) -> Project:
    return Project(
        name=name,
        root=root,
        queue_dir=root / "experiments" / "operator_queue",
    )


def _yaml(tmp_path: Path, project_name: str, task_id: str, **kwargs) -> Path:
    defaults = {
        "command": sys.executable,
        "args": ["-c", "print('hello')"],
        "resources": {"cpu_cores": 1},
        "priority": 50,
        "enabled": True,
    }
    defaults.update(kwargs)
    content = yaml.dump({"task_id": task_id, **defaults})
    p = tmp_path / f"{project_name}_{task_id}.yaml"
    p.write_text(content, encoding="utf-8")
    return p


def test_should_schedule_locally() -> None:
    assert should_schedule_locally("local") is True
    assert should_schedule_locally("any") is True
    assert should_schedule_locally("remote", online_workers=0) is True
    assert should_schedule_locally("remote", online_workers=1) is False
    assert should_schedule_locally("remote") is True  # default: no workers


def test_clock_jump_helpers() -> None:
    assert clock_jump_threshold(5) == 30.0
    assert clock_jump_threshold(20) == 40.0
    assert is_clock_jump(100.0, 140.0, 30.0) is True
    assert is_clock_jump(100.0, 120.0, 30.0) is False
    assert is_clock_jump(0.0, 999.0, 30.0) is False


def test_enable_task_sets_enabled_column(tmp_path: Path) -> None:
    db = open_db(tmp_path / "queue.db")
    proj = _project("p", tmp_path)
    path = _yaml(tmp_path, "p", "off", enabled=False)
    upsert_task(db, parse_task_yaml(path, proj))
    set_task_status(db, "p", "off", STATUS_DISABLED)
    db.execute("UPDATE tasks SET enabled = 0 WHERE project = ? AND task_id = ?", ("p", "off"))
    db.commit()

    ok, err = enable_task(db, "p", "off")
    assert ok is True
    assert err is None
    row = get_task(db, "p", "off")
    assert row["status"] == STATUS_QUEUED
    assert row["enabled"] == 1
    queued = get_queued_tasks(db)
    assert any(r["task_id"] == "off" for r in queued)


def test_enable_task_idempotent_when_already_queued(tmp_path: Path) -> None:
    db = open_db(tmp_path / "queue.db")
    proj = _project("p", tmp_path)
    path = _yaml(tmp_path, "p", "on", enabled=True)
    upsert_task(db, parse_task_yaml(path, proj))
    db.commit()

    ok, err = enable_task(db, "p", "on")
    assert ok is True
    assert err is None
    row = get_task(db, "p", "on")
    assert row["status"] == STATUS_QUEUED
    assert row["enabled"] == 1


def test_force_rejects_running(tmp_path: Path) -> None:
    db = open_db(tmp_path / "queue.db")
    proj = _project("p", tmp_path)
    path = _yaml(tmp_path, "p", "live")
    upsert_task(db, parse_task_yaml(path, proj))
    set_task_status(db, "p", "live", STATUS_RUNNING)

    err = force_task(db, "p", "live")
    assert err is not None
    assert "running" in err.lower()
    assert get_task(db, "p", "live")["status"] == STATUS_RUNNING


def test_force_ok_when_queued(tmp_path: Path) -> None:
    db = open_db(tmp_path / "queue.db")
    proj = _project("p", tmp_path)
    path = _yaml(tmp_path, "p", "q")
    upsert_task(db, parse_task_yaml(path, proj))
    assert force_task(db, "p", "q") is None
    assert get_task(db, "p", "q")["force_run"] == 1


def test_claim_task_running_is_atomic(tmp_path: Path) -> None:
    db = open_db(tmp_path / "queue.db")
    proj = _project("p", tmp_path)
    path = _yaml(tmp_path, "p", "race")
    upsert_task(db, parse_task_yaml(path, proj))
    assert claim_task_running(db, "p", "race") is True
    assert get_task(db, "p", "race")["status"] == STATUS_RUNNING
    assert claim_task_running(db, "p", "race") is False


def test_requeue_orphaned_running(tmp_path: Path) -> None:
    db = open_db(tmp_path / "queue.db")
    proj = _project("p", tmp_path)
    path = _yaml(tmp_path, "p", "ghost")
    upsert_task(db, parse_task_yaml(path, proj))
    set_task_status(db, "p", "ghost", STATUS_RUNNING)
    insert_run(db, "p", "ghost", str(tmp_path / "ghost.log"))

    n = requeue_orphaned_running(db, reason="clock-jump")
    assert n == 1
    assert get_task(db, "p", "ghost")["status"] == STATUS_QUEUED
    last = db.execute(
        "SELECT status FROM runs WHERE project = ? AND task_id = ? ORDER BY run_id DESC LIMIT 1",
        ("p", "ghost"),
    ).fetchone()
    assert last["status"] == "interrupted"

