"""Priority DESC scheduling + one-shot invert migration."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

import pytest
import yaml

from taskqueue.config import Project
from taskqueue._db_tasks import get_queued_tasks, get_task, upsert_task
from taskqueue.db import open_db, parse_task_yaml
from taskqueue.models import STATUS_COMPLETED
from taskqueue.priority_migrate import invert_priority, migrate_priority_invert
from taskqueue.resources import sort_queued_fairshare
from taskqueue.runner import ActiveRun
from tests.helpers.wait import wait_until


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


def test_invert_priority_clamp() -> None:
    assert invert_priority(40) == 60
    assert invert_priority(50) == 50
    assert invert_priority(0) == 100
    assert invert_priority(100) == 0
    assert invert_priority(150) == 0


def test_get_queued_tasks_higher_priority_first(tmp_path: Path) -> None:
    db = open_db(tmp_path / "queue.db")
    proj = _project("p", tmp_path)
    for tid, prio in (("low", 10), ("high", 90), ("mid", 50)):
        path = _yaml(tmp_path, "p", tid, priority=prio)
        upsert_task(db, parse_task_yaml(path, proj))
    queued = get_queued_tasks(db)
    assert [r["task_id"] for r in queued] == ["high", "mid", "low"]


def test_fairshare_higher_priority_beats_gpu_load() -> None:
    class R(dict):
        def __getitem__(self, k):
            return dict.__getitem__(self, k)

    queued = [
        R(project="heavy", task_id="a", priority=90, enqueued_at="2020-01-01T00:00:00"),
        R(project="light", task_id="b", priority=10, enqueued_at="2020-01-01T00:00:00"),
    ]
    ordered = sort_queued_fairshare(queued, {"heavy": 5, "light": 0})
    assert ordered[0]["task_id"] == "a"


def test_migrate_priority_invert_db_and_yaml(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    root = tmp_path / "proj"
    qdir = root / "experiments" / "operator_queue"
    qdir.mkdir(parents=True)
    yml = qdir / "t1.yaml"
    yml.write_text(
        "task_id: t1\ncommand: python\npriority: 40\nenabled: true\n",
        encoding="utf-8",
    )
    (home / "projects.yaml").write_text(
        "projects:\n"
        f"  p:\n"
        f"    root: {root}\n",
        encoding="utf-8",
    )
    db = open_db(home / "queue.db")
    proj = Project(name="p", root=root, queue_dir=qdir)
    upsert_task(db, parse_task_yaml(yml, proj))
    db.close()

    dry = migrate_priority_invert(home, dry_run=True)
    assert dry["ok"] and dry["dry_run"]
    assert dry["db_updated"] == 1
    assert dry["yaml_updated"] == 1

    applied = migrate_priority_invert(home, dry_run=False)
    assert applied["ok"] and not applied["dry_run"]

    db = open_db(home / "queue.db")
    row = db.execute(
        "SELECT priority FROM tasks WHERE project='p' AND task_id='t1'"
    ).fetchone()
    assert int(row["priority"]) == 60
    assert "priority: 60" in yml.read_text(encoding="utf-8")

    again = migrate_priority_invert(home, dry_run=False)
    assert again["ok"] is False
    assert "already migrated" in again["error"]


@pytest.mark.skipif(os.name != "nt", reason="CREATE_NO_WINDOW is Windows-only")
def test_windows_spawn_uses_create_no_window(tmp_path: Path, monkeypatch) -> None:
    db = open_db(tmp_path / "queue.db")
    proj = _project("p", tmp_path)
    path = _yaml(
        tmp_path,
        "p",
        "hide",
        command=sys.executable,
        args=["-c", "print('(10.0%) hi')"],
    )
    upsert_task(db, parse_task_yaml(path, proj))
    row = get_task(db, "p", "hide")
    run = ActiveRun(conn=db, task_row=row, log_dir=tmp_path / "logs", cwd=tmp_path)

    seen: dict = {}

    real_popen = subprocess.Popen

    def spy_popen(*args, **kwargs):
        seen["creationflags"] = kwargs.get("creationflags", 0)
        seen["stdout"] = kwargs.get("stdout")
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", spy_popen)
    assert run.start(use_resume=False) is None
    wait_until(run.poll, timeout=30, interval=0.05)
    assert run.poll()
    assert seen["creationflags"] & subprocess.CREATE_NO_WINDOW
    assert seen["stdout"] is not subprocess.PIPE
    assert run.progress_pct == 10.0
    run.finalize(STATUS_COMPLETED)
