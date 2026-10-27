"""Slice E: pause, max_attempts, yaml patch."""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

from taskqueue.config import Project
from taskqueue._db_tasks import (
    failed_attempt_count,
    get_task,
    retry_task,
    set_task_status,
    upsert_task,
)
from taskqueue._db_runs import finish_run, insert_run
from taskqueue.db import is_project_paused, open_db, parse_task_yaml, set_project_paused
from taskqueue.models import STATUS_FAILED, STATUS_STOPPED
from taskqueue.yaml_patch import patch_task_yaml


def test_project_pause(tmp_path: Path) -> None:
    db = open_db(tmp_path / "q.db")
    assert is_project_paused(db, "p") is False
    set_project_paused(db, "p", True)
    assert is_project_paused(db, "p") is True
    set_project_paused(db, "p", False)
    assert is_project_paused(db, "p") is False


def test_max_attempts_blocks_admit_but_explicit_retry_resets(tmp_path: Path) -> None:
    """Admit uses failed_attempt_count; F-123 explicit retry resets the baseline window."""
    from taskqueue._db_tasks import retry_skip_reason

    db = open_db(tmp_path / "q.db")
    proj = Project(name="p", root=tmp_path, queue_dir=tmp_path / "q")
    yml = tmp_path / "t.yaml"
    yml.write_text(
        yaml.dump({
            "task_id": "t",
            "command": sys.executable,
            "args": ["-c", "pass"],
            "max_attempts": 2,
        }),
        encoding="utf-8",
    )
    upsert_task(db, parse_task_yaml(yml, proj))
    for _ in range(2):
        rid = insert_run(db, "p", "t", str(tmp_path / "r.log"))
        finish_run(db, rid, 1, "failed")
    assert failed_attempt_count(db, "p", "t") == 2
    set_task_status(db, "p", "t", STATUS_FAILED)
    assert retry_skip_reason(db, "p", "t") == "max_attempts"
    assert retry_task(db, "p", "t") is True
    assert failed_attempt_count(db, "p", "t") == 0
    row = get_task(db, "p", "t")
    assert row["status"] == "queued"


def test_retry_clears_error_state(tmp_path: Path) -> None:
    db = open_db(tmp_path / "q.db")
    proj = Project(name="p", root=tmp_path, queue_dir=tmp_path / "q")
    yml = tmp_path / "t.yaml"
    yml.write_text(
        yaml.dump({"task_id": "t", "command": sys.executable, "args": ["-c", "pass"]}),
        encoding="utf-8",
    )
    upsert_task(db, parse_task_yaml(yml, proj))
    db.execute(
        "UPDATE tasks SET status = 'stopped', last_error_code = 'tq.stop.ram', "
        "block_reason = 'VRAM: device 9.7 > hard 5.7' WHERE project = 'p' AND task_id = 't'"
    )
    db.commit()
    assert retry_task(db, "p", "t") is True
    row = db.execute("SELECT status, last_error_code, block_reason FROM tasks WHERE task_id='t'").fetchone()
    assert row["status"] == "queued"
    assert row["last_error_code"] is None
    assert row["block_reason"] is None


def test_yaml_patch(tmp_path: Path) -> None:
    p = tmp_path / "t.yaml"
    p.write_text("task_id: t\ncommand: python\npriority: 50\nenabled: false\n", encoding="utf-8")
    applied = patch_task_yaml(p, {"priority": 10, "enabled": True, "max_attempts": 3})
    assert "priority" in applied and "max_attempts" in applied
    text = p.read_text(encoding="utf-8")
    assert "priority: 10" in text
    assert "enabled: true" in text
    assert "max_attempts: 3" in text
