"""Orphan PID reap on dirty daemon restart / clock-jump reclaim."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import psutil
import yaml

from taskqueue.config import Project
from taskqueue.daemon import requeue_orphaned_running
from taskqueue.db import SCHEMA_VERSION, open_db, parse_task_yaml
from taskqueue._db_tasks import get_task, set_task_status, upsert_task
from taskqueue._db_runs import insert_run, set_run_pid
from taskqueue.models import STATUS_QUEUED, STATUS_RUNNING
from taskqueue.reap import kill_process_tree


def _project(root: Path) -> Project:
    return Project(
        name="p",
        root=root,
        queue_dir=root / "experiments" / "operator_queue",
    )


def _upsert(db, root: Path, task_id: str) -> None:
    yml = root / f"{task_id}.yaml"
    yml.write_text(
        yaml.dump(
            {
                "task_id": task_id,
                "command": sys.executable,
                "args": ["-c", "pass"],
                "resources": {"cpu_cores": 1},
                "enabled": True,
            }
        ),
        encoding="utf-8",
    )
    upsert_task(db, parse_task_yaml(yml, _project(root)))


def test_schema_has_run_pid() -> None:
    assert SCHEMA_VERSION >= 9


def test_kill_process_tree_terminates(tmp_path: Path) -> None:
    proc = psutil.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        cwd=str(tmp_path),
    )
    try:
        assert proc.is_running()
        result = kill_process_tree(
            proc.pid,
            create_time=proc.create_time(),
            graceful_sec=2.0,
        )
        assert result == "killed"
        assert not psutil.pid_exists(proc.pid) or not proc.is_running()
    finally:
        try:
            proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass


def test_kill_process_tree_kills_descendants(tmp_path: Path) -> None:
    """Wrapper PID alone is not enough — grandchildren hold the real RSS."""
    child_marker = tmp_path / "child.pid"
    script = (
        "import subprocess, sys, time\n"
        f"open(r'{child_marker}', 'w').write('x')\n"
        "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        "open(r'{child_marker}', 'w').write(str(c.pid))\n"
        "time.sleep(120)\n"
    ).replace("{child_marker}", str(child_marker).replace("\\", "\\\\"))
    parent = psutil.Popen([sys.executable, "-c", script], cwd=str(tmp_path))
    try:
        deadline = time.time() + 10
        child_pid = None
        while time.time() < deadline:
            if child_marker.is_file():
                raw = child_marker.read_text(encoding="utf-8").strip()
                if raw.isdigit():
                    child_pid = int(raw)
                    break
            time.sleep(0.05)
        assert child_pid is not None and psutil.pid_exists(child_pid)
        result = kill_process_tree(
            parent.pid,
            create_time=parent.create_time(),
            graceful_sec=2.0,
        )
        assert result == "killed"
        deadline = time.time() + 5
        while time.time() < deadline and (
            parent.is_running() or psutil.pid_exists(child_pid)
        ):
            time.sleep(0.05)
        assert not parent.is_running()
        assert not psutil.pid_exists(child_pid)
    finally:
        try:
            kill_process_tree(parent.pid, graceful_sec=1.0)
        except Exception:
            pass


def test_kill_process_tree_skips_pid_reuse(tmp_path: Path) -> None:
    proc = psutil.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        cwd=str(tmp_path),
    )
    try:
        result = kill_process_tree(
            proc.pid,
            create_time=proc.create_time() - 9999.0,
            graceful_sec=1.0,
        )
        assert result == "skipped_reuse"
        assert proc.is_running()
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_requeue_reaps_orphan_pid(tmp_path: Path) -> None:
    db = open_db(tmp_path / "queue.db")
    _upsert(db, tmp_path, "ghost")
    set_task_status(db, "p", "ghost", STATUS_RUNNING)
    run_id = insert_run(db, "p", "ghost", str(tmp_path / "ghost.log"))

    child = psutil.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        cwd=str(tmp_path),
    )
    try:
        set_run_pid(db, run_id, child.pid, create_time=child.create_time())
        n = requeue_orphaned_running(db, reason="orphan", graceful_sec=2.0)
        assert n == 1
        assert get_task(db, "p", "ghost")["status"] == STATUS_QUEUED
        # Give OS a moment after kill.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and child.is_running():
            time.sleep(0.05)
        assert not child.is_running()
        row = db.execute(
            "SELECT status FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        assert row["status"] == "interrupted"
    finally:
        try:
            if child.is_running():
                child.kill()
                child.wait(timeout=3)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        db.close()


def test_requeue_spares_wrong_create_time(tmp_path: Path) -> None:
    db = open_db(tmp_path / "queue.db")
    _upsert(db, tmp_path, "ghost")
    set_task_status(db, "p", "ghost", STATUS_RUNNING)
    run_id = insert_run(db, "p", "ghost", str(tmp_path / "ghost.log"))
    child = psutil.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        cwd=str(tmp_path),
    )
    try:
        set_run_pid(db, run_id, child.pid, create_time=child.create_time() - 9999.0)
        n = requeue_orphaned_running(db, reason="orphan", graceful_sec=1.0)
        assert n == 1
        assert child.is_running(), "PID reuse guard must not kill wrong process"
    finally:
        child.kill()
        child.wait(timeout=5)
        db.close()
