"""Wave 7: retry_backoff_sec — exponential backoff between re-admissions of a failing task."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from tests.test_t026_parity import _ctx, _decide, _task, _twin_nodes
from tqdaemon import errors as tqerr
from tqdaemon._db_tasks import get_task, upsert_task
from tqdaemon.backoff import MAX_DELAY_SEC, backoff_delay, backoff_left
from tqdaemon.config import Project
from tqdaemon.db import open_db, parse_task_yaml


def test_delay_doubles_caps_and_jitters_stably() -> None:
    assert backoff_delay(0, 3) == 0 and backoff_delay(60, 0) == 0
    d1, d2, d3 = (backoff_delay(60, n, "p/t") for n in (1, 2, 3))
    assert 60 <= d1 <= 72 and 120 <= d2 <= 144 and 240 <= d3 <= 288  # base * 2**(n-1), +0..20 % jitter
    assert backoff_delay(60, 2, "p/t") == d2  # stable: the wait reason does not flap between ticks
    assert backoff_delay(60, 2, "p/u") != d2  # tasks that failed together do not retry in lock step
    assert backoff_delay(60, 40, "p/t") <= MAX_DELAY_SEC * 1.2


def _db(tmp_path: Path, backoff: int = 60):
    path = tmp_path / "t.yaml"
    path.write_text(
        yaml.dump({"task_id": "t", "command": sys.executable, "args": ["-c", "pass"], "retry_backoff_sec": backoff}),
        encoding="utf-8",
    )
    db = open_db(tmp_path / "q.db")
    upsert_task(db, parse_task_yaml(path, Project(name="p", root=tmp_path, queue_dir=tmp_path)))
    db.commit()
    return db


def _run(db, status: str, finished: datetime) -> None:
    iso = finished.isoformat()
    db.execute(
        "INSERT INTO runs (project, task_id, started_at, finished_at, status, exit_code, log_path) "
        "VALUES ('p', 't', ?, ?, ?, 1, '')",
        (iso, iso, status),
    )
    db.commit()


def test_backoff_left_counts_consecutive_failures_and_resets(tmp_path: Path) -> None:
    db = _db(tmp_path)
    now = datetime.now(UTC)
    assert get_task(db, "p", "t")["retry_backoff_sec"] == 60
    assert backoff_left(db, get_task(db, "p", "t")) == (0.0, 0)  # no runs yet
    _run(db, "failed", now - timedelta(seconds=10))
    left, n = backoff_left(db, get_task(db, "p", "t"), now=now.timestamp())
    assert n == 1 and 50 <= left <= 62
    _run(db, "failed", now - timedelta(seconds=10))
    left, n = backoff_left(db, get_task(db, "p", "t"), now=now.timestamp())
    assert n == 2 and 110 <= left <= 134
    assert backoff_left(db, get_task(db, "p", "t"), now=now.timestamp() + 400) == (0.0, 2)  # waited it out
    # An explicit `tq retry` moves the attempt baseline: no backoff for the operator's retry.
    db.execute("UPDATE tasks SET retry_attempt_baseline = 2 WHERE task_id = 't'")
    assert backoff_left(db, get_task(db, "p", "t"), now=now.timestamp()) == (0.0, 0)
    db.execute("UPDATE tasks SET retry_attempt_baseline = 0 WHERE task_id = 't'")
    # A success ends the failing streak.
    _run(db, "completed", now - timedelta(seconds=5))
    assert backoff_left(db, get_task(db, "p", "t"), now=now.timestamp()) == (0.0, 0)
    db.close()


def test_filter_blocks_while_backing_off_and_force_bypasses() -> None:
    local, _remote = _twin_nodes()
    blocked = _decide(_task(), local, _ctx(backoff_left_sec=42.2))
    assert not blocked.ok and blocked.code == tqerr.BLOCK_BACKOFF and "43s left" in blocked.reason
    assert _decide(_task(), local, _ctx(backoff_left_sec=42.2, force=True)).ok
    assert _decide(_task(), local, _ctx()).ok
    assert tqerr.block_kind(tqerr.BLOCK_BACKOFF) == "max_attempts"


def test_yaml_field_validation(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.dump({"task_id": "bad", "command": "x", "retry_backoff_sec": -5}), encoding="utf-8")
    with pytest.raises(ValueError, match="retry_backoff_sec"):
        parse_task_yaml(path, Project(name="p", root=tmp_path, queue_dir=tmp_path))


def test_local_admission_holds_a_backing_off_task(tmp_path: Path) -> None:
    from tests.test_t204_scavenger import _admit, _project

    proj = _project(tmp_path)
    p = proj.queue_dir / "t.yaml"
    p.write_text(
        yaml.dump({"task_id": "t", "command": sys.executable, "args": ["-c", "pass"], "retry_backoff_sec": 300}),
        encoding="utf-8",
    )
    db = open_db(tmp_path / "q.db")
    upsert_task(db, parse_task_yaml(p, proj))
    db.commit()
    _run(db, "failed", datetime.now(UTC) - timedelta(seconds=5))
    _admit(db, tmp_path, {"p": proj})
    row = get_task(db, "p", "t")
    assert row["status"] == "queued" and row["last_error_code"] == tqerr.BLOCK_BACKOFF
    assert "retry backoff" in row["block_reason"]
    assert tqerr.BLOCK_BACKOFF in tqerr.all_codes()
    db.close()
