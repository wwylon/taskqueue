"""Stable tq.* error code persistence."""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

from taskqueue import errors as tqerr
from taskqueue.config import Project
from taskqueue.db import SCHEMA_VERSION, open_db, parse_task_yaml
from taskqueue._db_tasks import clear_task_error, get_task, set_task_error, upsert_task


def _project(name: str, root: Path) -> Project:
    return Project(
        name=name,
        root=root,
        queue_dir=root / "experiments" / "operator_queue",
    )


def test_schema_version_is_current(tmp_path: Path) -> None:
    assert SCHEMA_VERSION >= 3
    db = open_db(tmp_path / "queue.db")
    cols = {r["name"] for r in db.execute("PRAGMA table_info(tasks)").fetchall()}
    assert "last_error_code" in cols
    assert "block_reason" in cols
    run_cols = {r["name"] for r in db.execute("PRAGMA table_info(runs)").fetchall()}
    assert "last_error_code" in run_cols
    assert "lease_expires_at" in run_cols


def test_set_and_clear_task_error(tmp_path: Path) -> None:
    db = open_db(tmp_path / "queue.db")
    proj = _project("p", tmp_path)
    path = tmp_path / "t.yaml"
    path.write_text(
        yaml.dump({
            "task_id": "t1",
            "command": sys.executable,
            "args": ["-c", "pass"],
            "resources": {"cpu_cores": 1},
        }),
        encoding="utf-8",
    )
    upsert_task(db, parse_task_yaml(path, proj))
    set_task_error(db, "p", "t1", tqerr.BLOCK_DEPS, block_reason="waiting on: a")
    row = get_task(db, "p", "t1")
    assert row["last_error_code"] == tqerr.BLOCK_DEPS
    assert row["block_reason"] == "waiting on: a"
    clear_task_error(db, "p", "t1")
    row = get_task(db, "p", "t1")
    assert row["last_error_code"] is None
    assert row["block_reason"] is None


def test_error_catalog_covers_block_and_run() -> None:
    codes = tqerr.all_codes()
    assert tqerr.BLOCK_RESOURCES in codes
    assert tqerr.RUN_NONZERO in codes
    assert tqerr.INFRA_CLOCK_JUMP in codes
    assert tqerr.INFRA_CLOCK_JUMP_FLAP in codes
    assert "cpu" in tqerr.describe(tqerr.BLOCK_RESOURCES).lower()
