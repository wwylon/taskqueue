"""Clock-jump reclaim: stop_reason, anti-flap, and slow-tick helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from taskqueue import errors as tqerr
from taskqueue.models import STATUS_INTERRUPTED, STATUS_QUEUED, STATUS_RUNNING
from taskqueue.daemon import _count_recent_clock_jump_runs, is_clock_jump, requeue_orphaned_running
from taskqueue._db_tasks import get_task, set_task_status, upsert_task
from taskqueue._db_runs import insert_run
from taskqueue.db import open_db, parse_task_yaml, set_config


def _project(name: str, tmp_path: Path):
    from taskqueue.config import Project

    root = tmp_path / name
    root.mkdir()
    (root / "experiments" / "operator_queue").mkdir(parents=True)
    return Project(name=name, root=root, queue_dir=root / "experiments" / "operator_queue")


def _yaml(tmp_path: Path, project: str, task_id: str) -> Path:
    q = tmp_path / project / "experiments" / "operator_queue"
    q.mkdir(parents=True, exist_ok=True)
    path = q / f"{task_id}.yaml"
    path.write_text(
        f"id: {task_id}\ncommand: echo\nargs: [hi]\n",
        encoding="utf-8",
    )
    return path


def test_requeue_clock_jump_sets_stop_reason(tmp_path: Path) -> None:
    db = open_db(tmp_path / "queue.db")
    proj = _project("p", tmp_path)
    path = _yaml(tmp_path, "p", "t1")
    upsert_task(db, parse_task_yaml(path, proj))
    set_task_status(db, "p", "t1", STATUS_RUNNING)
    insert_run(db, "p", "t1", str(tmp_path / "t1.log"))

    requeue_orphaned_running(db, reason="clock-jump", gap_sec=185.0)

    run = db.execute(
        "SELECT stop_reason, last_error_code, stopped_by FROM runs WHERE project='p'"
    ).fetchone()
    assert run["stop_reason"] == "clock-jump:gap=185s"
    assert run["last_error_code"] == tqerr.INFRA_CLOCK_JUMP
    assert run["stopped_by"] == "host-sleep"
    assert get_task(db, "p", "t1")["status"] == STATUS_QUEUED


def test_clock_jump_flap_leaves_interrupted(tmp_path: Path) -> None:
    db = open_db(tmp_path / "queue.db")
    set_config(db, "clock_jump_max_requeue", 2)
    set_config(db, "clock_jump_requeue_window_sec", 3600)
    proj = _project("p", tmp_path)
    path = _yaml(tmp_path, "p", "flap")
    upsert_task(db, parse_task_yaml(path, proj))

    now = datetime.now(UTC).isoformat()
    for i in range(2):
        db.execute(
            "INSERT INTO runs (project, task_id, started_at, finished_at, status, "
            "exit_code, log_path, last_error_code) "
            "VALUES ('p', 'flap', ?, ?, 'interrupted', -1, '', ?)",
            (now, now, tqerr.INFRA_CLOCK_JUMP),
        )
    db.commit()

    set_task_status(db, "p", "flap", STATUS_RUNNING)
    insert_run(db, "p", "flap", str(tmp_path / "flap.log"))

    n = requeue_orphaned_running(db, reason="clock-jump", gap_sec=200.0)
    assert n == 1
    task = db.execute(
        "SELECT status, last_error_code, block_reason FROM tasks WHERE project='p'"
    ).fetchone()
    assert task["status"] == STATUS_INTERRUPTED
    assert task["last_error_code"] == tqerr.INFRA_CLOCK_JUMP_FLAP
    assert "clock-jump flap" in (task["block_reason"] or "")


def test_clock_jump_flap_disabled_when_max_zero(tmp_path: Path) -> None:
    db = open_db(tmp_path / "queue.db")
    set_config(db, "clock_jump_max_requeue", 0)
    proj = _project("p", tmp_path)
    path = _yaml(tmp_path, "p", "free")
    upsert_task(db, parse_task_yaml(path, proj))
    now = datetime.now(UTC).isoformat()
    for _ in range(5):
        db.execute(
            "INSERT INTO runs (project, task_id, started_at, finished_at, status, "
            "exit_code, log_path, last_error_code) "
            "VALUES ('p', 'free', ?, ?, 'interrupted', -1, '', ?)",
            (now, now, tqerr.INFRA_CLOCK_JUMP),
        )
    db.commit()
    set_task_status(db, "p", "free", STATUS_RUNNING)
    insert_run(db, "p", "free", str(tmp_path / "free.log"))

    requeue_orphaned_running(db, reason="clock-jump")
    assert get_task(db, "p", "free")["status"] == STATUS_QUEUED


def test_count_recent_clock_jump_runs(tmp_path: Path) -> None:
    db = open_db(tmp_path / "queue.db")
    now = datetime.now(UTC).isoformat()
    db.execute(
        "INSERT INTO runs (project, task_id, started_at, finished_at, status, "
        "exit_code, log_path, last_error_code) "
        "VALUES ('p', 't', ?, ?, 'interrupted', -1, '', ?)",
        (now, now, tqerr.INFRA_CLOCK_JUMP),
    )
    db.commit()
    assert _count_recent_clock_jump_runs(db, "p", "t", 3600) == 1
    assert _count_recent_clock_jump_runs(db, "p", "other", 3600) == 0


def test_count_recent_clock_jump_runs_excludes_old_same_day(tmp_path: Path) -> None:
    from datetime import timedelta

    db = open_db(tmp_path / "queue.db")
    old = (datetime.now(UTC) - timedelta(seconds=1800)).isoformat()
    db.execute(
        "INSERT INTO runs (project, task_id, started_at, finished_at, status, "
        "exit_code, log_path, last_error_code) "
        "VALUES ('p', 't', ?, ?, 'interrupted', -1, '', ?)",
        (old, old, tqerr.INFRA_CLOCK_JUMP),
    )
    db.commit()
    assert _count_recent_clock_jump_runs(db, "p", "t", 600) == 0
    assert _count_recent_clock_jump_runs(db, "p", "t", 3600) == 1


def test_is_clock_jump_threshold() -> None:
    assert is_clock_jump(100.0, 250.0, 120.0) is True
    assert is_clock_jump(100.0, 210.0, 120.0) is False
