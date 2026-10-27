"""Cooperative suspend (exit 75) + control file helpers."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

from taskqueue.config import Project, control_path, state_home
from taskqueue._db_tasks import (
    get_task,
    retry_task,
    set_task_status,
    upsert_task,
    write_control_action,
)
from taskqueue.db import open_db, parse_task_yaml
from taskqueue.models import EXIT_SUSPENDED, STATUS_QUEUED, STATUS_SUSPENDED
from taskqueue.runner import ActiveRun
from tests.helpers.wait import wait_until


def _project(tmp_path: Path) -> Project:
    return Project(
        name="p",
        root=tmp_path,
        queue_dir=tmp_path / "q",
    )


def test_write_control_action(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TASKQUEUE_HOME", str(tmp_path / "home"))
    home = state_home()
    path = write_control_action(home, "p", "t1", "suspend")
    assert path == control_path(home, "p", "t1")
    assert json.loads(path.read_text(encoding="utf-8"))["action"] == "suspend"


def test_exit_75_marks_suspended(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("TASKQUEUE_HOME", str(home))
    db = open_db(home / "queue.db")
    proj = _project(tmp_path)
    script = tmp_path / "suspend_me.py"
    script.write_text(
        "import os, time\n"
        "from pathlib import Path\n"
        "p = Path(os.environ['TQ_CONTROL_FILE'])\n"
        "for _ in range(50):\n"
        "    if p.is_file() and 'suspend' in p.read_text(encoding='utf-8'):\n"
        "        print('(50.0%)'); raise SystemExit(75)\n"
        "    time.sleep(0.05)\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    yml = tmp_path / "t.yaml"
    yml.write_text(
        yaml.dump({
            "task_id": "suspend_me",
            "command": sys.executable,
            "args": [str(script)],
            "resources": {"cpu_cores": 1},
        }),
        encoding="utf-8",
    )
    upsert_task(db, parse_task_yaml(yml, proj))
    row = get_task(db, "p", "suspend_me")
    run = ActiveRun(
        conn=db,
        task_row=row,
        log_dir=tmp_path / "logs",
        cwd=tmp_path,
        db_path=home / "queue.db",
    )
    assert run.start(use_resume=False) is None
    write_control_action(home, "p", "suspend_me", "suspend")
    import time
    wait_until(run.poll, timeout=30, interval=0.05)
    assert run.poll()
    assert run._exit_code == EXIT_SUSPENDED
    run.finalize(STATUS_SUSPENDED)
    assert get_task(db, "p", "suspend_me")["status"] == STATUS_SUSPENDED
    assert retry_task(db, "p", "suspend_me") is True
    assert get_task(db, "p", "suspend_me")["status"] == STATUS_QUEUED
