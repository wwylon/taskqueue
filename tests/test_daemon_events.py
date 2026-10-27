"""Daemon lifecycle events: dirty/clean boot classification and helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from taskqueue import config as tqconfig
from taskqueue import errors as tqerr
from taskqueue.daemon import classify_daemon_boot, requeue_orphaned_running
from taskqueue.db import (
    DAEMON_EVENT_CLEAN_SHUTDOWN,
    DAEMON_EVENT_DIRTY_RESTART,
    DAEMON_EVENT_START,
    SCHEMA_VERSION,
    insert_daemon_event,
    last_boot_kind,
    list_daemon_events,
    open_db,
    parse_task_yaml,
    prune_daemon_events,
)
from taskqueue._db_runs import finish_run, insert_run
from taskqueue._db_tasks import get_task, set_task_status, upsert_task
from taskqueue.models import STATUS_QUEUED, STATUS_RUNNING


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


def test_schema_has_daemon_events(tmp_path: Path) -> None:
    assert SCHEMA_VERSION >= 15
    db = open_db(tmp_path / "queue.db")
    cols = {r["name"] for r in db.execute("PRAGMA table_info(daemon_events)").fetchall()}
    assert "kind" in cols and "ts" in cols


def test_first_boot_not_dirty(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    db = open_db(home / "queue.db")
    boot = classify_daemon_boot(home, db)
    assert boot.dirty is False
    assert boot.prev_pid is None
    assert boot.had_clean is False


def test_stale_pid_without_clean_is_dirty(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    db = open_db(home / "queue.db")
    tqconfig.pid_path(home).write_text("99999999", encoding="utf-8")
    boot = classify_daemon_boot(home, db)
    assert boot.dirty is True
    assert boot.prev_pid == 99999999
    assert boot.had_clean is False


def test_stale_pid_with_clean_not_dirty(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    db = open_db(home / "queue.db")
    tqconfig.pid_path(home).write_text("99999999", encoding="utf-8")
    tqconfig.clean_shutdown_path(home).write_text("ok", encoding="utf-8")
    boot = classify_daemon_boot(home, db)
    assert boot.dirty is False


def test_missing_pid_after_unclean_start_is_dirty(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    db = open_db(home / "queue.db")
    insert_daemon_event(db, DAEMON_EVENT_START, pid=1, detail={"dirty": False})
    boot = classify_daemon_boot(home, db)
    assert boot.dirty is True
    assert boot.last_kind == DAEMON_EVENT_START


def test_missing_pid_after_clean_shutdown_not_dirty(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    db = open_db(home / "queue.db")
    insert_daemon_event(db, DAEMON_EVENT_START, pid=1)
    insert_daemon_event(db, DAEMON_EVENT_CLEAN_SHUTDOWN, pid=1)
    boot = classify_daemon_boot(home, db)
    assert boot.dirty is False


def test_last_boot_kind_dirty_sequence(tmp_path: Path) -> None:
    db = open_db(tmp_path / "queue.db")
    insert_daemon_event(db, DAEMON_EVENT_DIRTY_RESTART, pid=1, detail={"prev_pid": 42})
    insert_daemon_event(db, DAEMON_EVENT_START, pid=2, detail={"dirty": True})
    assert last_boot_kind(db) == "dirty"


def test_last_boot_kind_clean_sequence(tmp_path: Path) -> None:
    db = open_db(tmp_path / "queue.db")
    insert_daemon_event(db, DAEMON_EVENT_CLEAN_SHUTDOWN, pid=1)
    insert_daemon_event(db, DAEMON_EVENT_START, pid=2, detail={"dirty": False})
    assert last_boot_kind(db) == "clean"


def test_prune_daemon_events(tmp_path: Path) -> None:
    db = open_db(tmp_path / "queue.db")
    old_ts = (datetime.now(UTC) - timedelta(days=120)).isoformat()
    db.execute(
        "INSERT INTO daemon_events (ts, kind, pid) VALUES (?, ?, ?)",
        (old_ts, DAEMON_EVENT_START, 1),
    )
    db.commit()
    insert_daemon_event(db, DAEMON_EVENT_START, pid=2)
    deleted = prune_daemon_events(db, keep_days=90)
    assert deleted == 1
    assert len(list_daemon_events(db, limit=10)) == 1


def test_orphan_requeue_sets_infra_orphan(tmp_path: Path) -> None:
    db = open_db(tmp_path / "queue.db")
    proj = _project("p", tmp_path)
    path = _yaml(tmp_path, "p", "t1")
    upsert_task(db, parse_task_yaml(path, proj))
    set_task_status(db, "p", "t1", STATUS_RUNNING)
    insert_run(db, "p", "t1", str(tmp_path / "t1.log"))

    n = requeue_orphaned_running(db, reason="orphan")
    assert n == 1
    run = db.execute(
        "SELECT stop_reason, last_error_code, stopped_by, status FROM runs WHERE project='p'"
    ).fetchone()
    assert run["stop_reason"] == "orphan"
    assert run["last_error_code"] == tqerr.INFRA_ORPHAN
    assert run["stopped_by"] == "tq"
    assert run["status"] == "interrupted"
    assert get_task(db, "p", "t1")["status"] == STATUS_QUEUED


def test_finish_run_persists_stop_reason(tmp_path: Path) -> None:
    db = open_db(tmp_path / "queue.db")
    rid = insert_run(db, "p", "t", str(tmp_path / "t.log"))
    finish_run(
        db, rid, 15, "interrupted",
        last_error_code=tqerr.RUN_CAP,
        stopped_by="tq",
        stop_reason="wall_clock_cap",
    )
    row = db.execute(
        "SELECT stop_reason, stopped_by, last_error_code FROM runs WHERE run_id=?",
        (rid,),
    ).fetchone()
    assert row["stop_reason"] == "wall_clock_cap"
    assert row["stopped_by"] == "tq"
    assert row["last_error_code"] == tqerr.RUN_CAP


def test_finish_run_preserves_cli_stop_reason(tmp_path: Path) -> None:
    db = open_db(tmp_path / "queue.db")
    rid = insert_run(db, "p", "t", str(tmp_path / "t.log"))
    db.execute("UPDATE runs SET stop_reason = ? WHERE run_id = ?", ("user said so", rid))
    db.commit()
    finish_run(
        db, rid, 15, "interrupted",
        stopped_by="user",
        stop_reason="stop_requested",
    )
    row = db.execute("SELECT stop_reason FROM runs WHERE run_id=?", (rid,)).fetchone()
    assert row["stop_reason"] == "user said so"
