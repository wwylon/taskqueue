"""8.1 HIGH: a post-spawn failure in ActiveRun.start must not leave an orphan child running."""
from __future__ import annotations

import sys
from pathlib import Path

import psutil
import yaml

from taskqueue._db_tasks import get_task, upsert_task
from taskqueue.config import Project
from taskqueue.db import open_db, parse_task_yaml
from taskqueue.runner import ActiveRun


def test_failed_start_after_spawn_kills_child(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("TASKQUEUE_HOME", str(home))
    db = open_db(home / "queue.db")
    proj = Project(name="p", root=tmp_path, queue_dir=tmp_path / "q")
    script = tmp_path / "sleeper.py"
    script.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    yml = tmp_path / "t.yaml"
    yml.write_text(
        yaml.dump({"task_id": "orphan", "command": sys.executable, "args": [str(script)], "resources": {"cpu_cores": 1}}),
        encoding="utf-8",
    )
    upsert_task(db, parse_task_yaml(yml, proj))
    row = get_task(db, "p", "orphan")
    run = ActiveRun(conn=db, task_row=row, log_dir=tmp_path / "logs", cwd=tmp_path, db_path=home / "queue.db")

    def boom(*a, **k):
        raise RuntimeError("post-spawn failure")

    monkeypatch.setattr("taskqueue.db.set_run_pid", boom)
    err = run.start(use_resume=False)
    assert err and "post-spawn failure" in err
    assert run.proc is not None
    pid = run.proc.pid
    try:
        psutil.Process(pid).wait(timeout=10)
    except psutil.NoSuchProcess:
        pass
    assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
