"""Wall-clock cap classification, auto-retry, and batch retry."""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

from tqdaemon import errors as tqerr
from tqdaemon._db_tasks import (
    apply_cap_auto_retry,
    get_task,
    retry_task,
    retry_tasks_batch,
    set_task_status,
    upsert_task,
)
from tqdaemon.config import Project
from tqdaemon.db import SCHEMA_VERSION, open_db, parse_task_yaml
from tqdaemon.models import STATUS_INTERRUPTED, STATUS_QUEUED


def _project(name: str, root: Path) -> Project:
    return Project(
        name=name,
        root=root,
        queue_dir=root / "experiments" / "operator_queue",
    )


def _write_task(
    root: Path,
    task_id: str,
    *,
    auto_retry: bool = False,
    max_auto: int = 20,
    semantics: str = "checkpointable",
) -> Path:
    path = root / f"{task_id}.yaml"
    raw = {
        "task_id": task_id,
        "command": sys.executable,
        "args": ["-c", "pass"],
        "resume_args": ["-c", "pass", "--resume"],
        "resources": {"cpu_cores": 1},
        "execution_semantics": semantics,
        "wall_clock_cap_sec": 60,
    }
    if auto_retry:
        raw["auto_retry_on_cap"] = True
        raw["max_auto_retries"] = max_auto
    path.write_text(yaml.dump(raw), encoding="utf-8")
    return path


def test_schema_has_cap_auto_retry_columns(tmp_path: Path) -> None:
    assert SCHEMA_VERSION >= 16
    db = open_db(tmp_path / "queue.db")
    cols = {r["name"] for r in db.execute("PRAGMA table_info(tasks)").fetchall()}
    assert "auto_retry_on_cap" in cols
    assert "max_auto_retries" in cols
    assert "cap_auto_retry_count" in cols


def test_parse_auto_retry_on_cap_yaml(tmp_path: Path) -> None:
    proj = _project("p", tmp_path)
    path = _write_task(tmp_path, "t1", auto_retry=True, max_auto=5)
    task = parse_task_yaml(path, proj)
    assert task.auto_retry_on_cap is True
    assert task.max_auto_retries == 5
    db = open_db(tmp_path / "queue.db")
    upsert_task(db, task)
    row = get_task(db, "p", "t1")
    assert int(row["auto_retry_on_cap"]) == 1
    assert int(row["max_auto_retries"]) == 5
    assert int(row["cap_auto_retry_count"]) == 0


def test_apply_cap_auto_retry_requeues(tmp_path: Path) -> None:
    proj = _project("p", tmp_path)
    db = open_db(tmp_path / "queue.db")
    upsert_task(db, parse_task_yaml(_write_task(tmp_path, "t1", auto_retry=True), proj))
    set_task_status(db, "p", "t1", STATUS_INTERRUPTED)
    db.execute(
        "UPDATE tasks SET last_error_code = ? WHERE project = ? AND task_id = ?",
        (tqerr.RUN_CAP, "p", "t1"),
    )
    db.commit()
    assert apply_cap_auto_retry(db, "p", "t1") == "queued"
    row = get_task(db, "p", "t1")
    assert row["status"] == STATUS_QUEUED
    assert int(row["cap_auto_retry_count"]) == 1
    assert row["last_error_code"] == tqerr.RUN_CAP


def test_apply_cap_auto_retry_exhausted(tmp_path: Path) -> None:
    proj = _project("p", tmp_path)
    db = open_db(tmp_path / "queue.db")
    upsert_task(
        db,
        parse_task_yaml(_write_task(tmp_path, "t1", auto_retry=True, max_auto=2), proj),
    )
    set_task_status(db, "p", "t1", STATUS_INTERRUPTED)
    db.execute(
        "UPDATE tasks SET last_error_code = ?, cap_auto_retry_count = 2 WHERE project = ? AND task_id = ?",
        (tqerr.RUN_CAP, "p", "t1"),
    )
    db.commit()
    assert apply_cap_auto_retry(db, "p", "t1") == "exhausted"
    row = get_task(db, "p", "t1")
    assert row["status"] == STATUS_INTERRUPTED
    assert "cap_auto_retries exhausted" in (row["block_reason"] or "")


def test_apply_cap_auto_retry_skipped_without_opt_in(tmp_path: Path) -> None:
    proj = _project("p", tmp_path)
    db = open_db(tmp_path / "queue.db")
    upsert_task(db, parse_task_yaml(_write_task(tmp_path, "t1"), proj))
    set_task_status(db, "p", "t1", STATUS_INTERRUPTED)
    assert apply_cap_auto_retry(db, "p", "t1") == "skipped"


def test_manual_retry_resets_cap_counter(tmp_path: Path) -> None:
    proj = _project("p", tmp_path)
    db = open_db(tmp_path / "queue.db")
    upsert_task(db, parse_task_yaml(_write_task(tmp_path, "t1", auto_retry=True), proj))
    set_task_status(db, "p", "t1", STATUS_INTERRUPTED)
    db.execute(
        "UPDATE tasks SET cap_auto_retry_count = 7 WHERE project = ? AND task_id = ?",
        ("p", "t1"),
    )
    db.commit()
    assert retry_task(db, "p", "t1")
    row = get_task(db, "p", "t1")
    assert row["status"] == STATUS_QUEUED
    assert int(row["cap_auto_retry_count"]) == 0


def test_retry_tasks_batch_one_transaction(tmp_path: Path) -> None:
    proj = _project("p", tmp_path)
    db = open_db(tmp_path / "queue.db")
    for tid in ("a", "b", "c"):
        upsert_task(db, parse_task_yaml(_write_task(tmp_path, tid), proj))
        set_task_status(db, "p", tid, "failed")
    result = retry_tasks_batch(db, [("p", "a"), ("p", "b"), ("p", "c"), ("p", "missing")])
    assert set(result["requeued"]) == {"p/a", "p/b", "p/c"}
    assert any(s["key"] == "p/missing" for s in result["skipped"])
    for tid in ("a", "b", "c"):
        assert get_task(db, "p", tid)["status"] == STATUS_QUEUED


def test_apply_cap_auto_retry_skips_disabled_task(tmp_path: Path) -> None:
    proj = _project("p", tmp_path)
    db = open_db(tmp_path / "queue.db")
    upsert_task(db, parse_task_yaml(_write_task(tmp_path, "t1", auto_retry=True), proj))
    set_task_status(db, "p", "t1", "disabled")
    db.commit()
    assert apply_cap_auto_retry(db, "p", "t1") == "skipped"
    assert get_task(db, "p", "t1")["status"] == "disabled"
