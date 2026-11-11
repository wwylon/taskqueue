"""F-221: local finalize clears stop_requested and the error in the finalize transaction."""

from __future__ import annotations

from pathlib import Path

from tqdaemon._db_runs import finalize_run_and_task, insert_run
from tqdaemon.db import open_db


def _running(conn, tid: str, **cols) -> int:
    conn.execute(
        "INSERT INTO tasks (project, task_id, command, status, enqueued_at, updated_at, stop_requested, "
        "last_error_code, block_reason, cap_auto_retry_count) "
        "VALUES ('p', ?, 'x', 'running', '2026-01-01', '2026-01-01', ?, ?, ?, ?)",
        (tid, cols.get("stop", 1), cols.get("code"), cols.get("br"), cols.get("cap", 3)),
    )
    conn.commit()
    return insert_run(conn, "p", tid, log_path="x.log")


def _row(conn, tid):
    return conn.execute("SELECT * FROM tasks WHERE project='p' AND task_id=?", (tid,)).fetchone()


def test_completed_clears_error_stop_and_cap_counter(tmp_path: Path) -> None:
    db = open_db(tmp_path / "q.db")
    rid = _running(db, "t", code="tq.block.resources", br="old")
    assert finalize_run_and_task(db, rid, "p", "t", 0, "completed", clear_task_error=True, clear_stop=True)
    row = _row(db, "t")
    assert (row["status"], row["stop_requested"], row["last_error_code"], row["block_reason"]) == (
        "completed",
        0,
        None,
        None,
    )
    assert row["cap_auto_retry_count"] == 0
    assert not db.in_transaction


def test_error_keeps_rich_block_reason_or_fills_description(tmp_path: Path) -> None:
    db = open_db(tmp_path / "q.db")
    rid = _running(db, "a", br="RAM: host 60 > hard 57")
    assert finalize_run_and_task(db, rid, "p", "a", 1, "stopped", last_error_code="tq.stop.ram", clear_stop=True)
    row = _row(db, "a")
    assert row["last_error_code"] == "tq.stop.ram" and row["block_reason"].startswith("RAM: host")
    assert row["stop_requested"] == 0 and row["cap_auto_retry_count"] == 3

    rid = _running(db, "b")
    assert finalize_run_and_task(db, rid, "p", "b", 2, "failed", last_error_code="tq.run.nonzero")
    row = _row(db, "b")
    assert row["block_reason"]  # filled from errors.describe
    assert row["stop_requested"] == 1  # clear_stop not requested
