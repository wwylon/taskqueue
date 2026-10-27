"""T-206: ``on_dependency_failure: cancel``."""

from __future__ import annotations

from pathlib import Path

import pytest

from taskqueue import errors as tqerr
from taskqueue._db_tasks import (
    RETRYABLE_STATUSES,
    retry_task_with_downstream,
    upsert_task,
)
from taskqueue.config import Project
from taskqueue.db import open_db, parse_task_yaml
from taskqueue.queries import cancel_dead_dependents
from taskqueue.waiting import SETTLED_STATUSES


def _project(tmp_path: Path) -> Project:
    root = tmp_path / "p"
    qdir = root / "experiments" / "operator_queue"
    qdir.mkdir(parents=True)
    return Project(name="p", root=root, queue_dir=qdir)


def _add(db, proj: Project, task_id: str, body: str = "") -> None:
    path = proj.queue_dir / f"{task_id}.yaml"
    path.write_text(
        f"task_id: {task_id}\ncommand: python\nargs: ['-c', 'pass']\n{body}", encoding="utf-8"
    )
    upsert_task(db, parse_task_yaml(path, proj))


def _st(db, task_id: str) -> str:
    return db.execute("SELECT status FROM tasks WHERE task_id=?", (task_id,)).fetchone()["status"]


def _set(db, task_id: str, status: str) -> None:
    db.execute("UPDATE tasks SET status=? WHERE task_id=?", (status, task_id))
    db.commit()


def test_parse_validation(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    p = proj.queue_dir / "x.yaml"
    p.write_text("task_id: x\ncommand: python\non_dependency_failure: nope\n", encoding="utf-8")
    with pytest.raises(ValueError, match="on_dependency_failure"):
        parse_task_yaml(p, proj)


def test_block_default_is_untouched(tmp_path: Path) -> None:
    proj, db = _project(tmp_path), open_db(tmp_path / "q.db")
    _add(db, proj, "gate")
    _add(db, proj, "next", "depends_on: [gate]\n")
    _set(db, "gate", "failed")
    assert cancel_dead_dependents(db) == []
    assert _st(db, "next") == "queued"


def test_cancel_and_cascade(tmp_path: Path) -> None:
    proj, db = _project(tmp_path), open_db(tmp_path / "q.db")
    _add(db, proj, "gate")
    _add(db, proj, "b", "depends_on: [gate]\non_dependency_failure: cancel\n")
    _add(db, proj, "c", "depends_on: [b]\non_dependency_failure: cancel\n")
    _set(db, "gate", "failed")
    assert cancel_dead_dependents(db) == [("p", "b")]
    row = db.execute("SELECT * FROM tasks WHERE task_id='b'").fetchone()
    assert row["status"] == "cancelled" and row["last_error_code"] == tqerr.DEP_CANCELLED
    assert "gate" in row["block_reason"]
    assert cancel_dead_dependents(db) == [("p", "c")]  # next hop on the next tick
    assert _st(db, "c") == "cancelled"


def test_stopped_or_disabled_dependency_does_not_cancel(tmp_path: Path) -> None:
    proj, db = _project(tmp_path), open_db(tmp_path / "q.db")
    _add(db, proj, "gate")
    _add(db, proj, "b", "depends_on: [gate]\non_dependency_failure: cancel\n")
    for st in ("stopped", "interrupted", "disabled", "running", "completed"):
        _set(db, "gate", st)
        assert cancel_dead_dependents(db) == [], st


def test_depends_on_any_cancels_only_when_all_dead(tmp_path: Path) -> None:
    proj, db = _project(tmp_path), open_db(tmp_path / "q.db")
    _add(db, proj, "a")
    _add(db, proj, "b")
    _add(db, proj, "c", "depends_on_any: [a, b]\non_dependency_failure: cancel\n")
    _set(db, "a", "failed")
    assert cancel_dead_dependents(db) == []
    _set(db, "b", "failed")
    assert cancel_dead_dependents(db) == [("p", "c")]


def test_retry_downstream_revives_cancelled(tmp_path: Path) -> None:
    proj, db = _project(tmp_path), open_db(tmp_path / "q.db")
    _add(db, proj, "gate")
    _add(db, proj, "b", "depends_on: [gate]\non_dependency_failure: cancel\n")
    _set(db, "gate", "failed")
    cancel_dead_dependents(db)
    assert "cancelled" in RETRYABLE_STATUSES and "cancelled" in SETTLED_STATUSES
    retry_task_with_downstream(db, "p", "gate")
    assert _st(db, "gate") == "queued" and _st(db, "b") == "queued"


def test_cancel_emits_notify_event(tmp_path: Path) -> None:
    from taskqueue.notify import iter_notify_events, notify_log_path

    proj, db = _project(tmp_path), open_db(tmp_path / "q.db")
    _add(db, proj, "gate")
    _add(db, proj, "b", "depends_on: [gate]\non_dependency_failure: cancel\n")
    _set(db, "gate", "failed")
    cancel_dead_dependents(db, tmp_path, {})
    evs = [e for e in iter_notify_events(notify_log_path(tmp_path)) if e.get("task_id") == "b"]
    assert evs and evs[-1]["status"] == "cancelled"
