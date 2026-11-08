"""F-241: ~/.taskqueue/locks/gpu.lock refcount / stale scrub."""

from __future__ import annotations

import json
import os
from pathlib import Path

from taskqueue.config import gpu_lock_path
from taskqueue.gpu_lock import acquire, release, scrub_stale, sync_from_holders


def test_acquire_release_refcount(tmp_path: Path) -> None:
    home = tmp_path
    path = gpu_lock_path(home)
    acquire(home, project="p", task_id="a", run_id=1, pid=os.getpid(), started=1.0)
    assert path.is_file()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["project"] == "p" and data["task_id"] == "a" and data["run_id"] == 1
    acquire(home, project="p", task_id="b", run_id=2, pid=os.getpid(), started=2.0)
    assert path.is_file()
    assert len(json.loads(path.read_text(encoding="utf-8"))["holders"]) == 2
    release(home, project="p", task_id="a", run_id=1)
    assert path.is_file()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["task_id"] == "b" and len(data["holders"]) == 1
    release(home, project="p", task_id="b", run_id=2)
    assert not path.exists()


def test_scrub_stale_removes_dead_pid(tmp_path: Path) -> None:
    home = tmp_path
    path = gpu_lock_path(home)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "project": "p",
                "task_id": "dead",
                "run_id": 9,
                "pid": 1_999_999_999,  # almost certainly not alive
                "started": 0,
                "holders": [
                    {
                        "project": "p",
                        "task_id": "dead",
                        "run_id": 9,
                        "pid": 1_999_999_999,
                        "started": 0,
                    },
                    {
                        "project": "p",
                        "task_id": "live",
                        "run_id": 1,
                        "pid": os.getpid(),
                        "started": 1,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    removed = scrub_stale(home)
    assert removed == 1
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["task_id"] == "live" and len(data["holders"]) == 1


def test_sync_from_holders_empty_deletes(tmp_path: Path) -> None:
    home = tmp_path
    acquire(home, project="p", task_id="a", run_id=1, pid=os.getpid())
    sync_from_holders(home, [])
    assert not gpu_lock_path(home).exists()
