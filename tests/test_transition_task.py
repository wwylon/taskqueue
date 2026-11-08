"""Wave 4 leftover: one guarded helper for task status changes (transition_task)."""

from __future__ import annotations

import pytest

from taskqueue._db_tasks import get_task, transition_task
from tests.helpers.master_worker import make_handler_env, seed_queued_task


def test_guarded_move_stamps_times_and_fields(tmp_path) -> None:
    _h, db, _ = make_handler_env(tmp_path)
    seed_queued_task(db, project="p", task_id="t", dispatch="local")
    db.execute("UPDATE tasks SET stop_requested = 1, block_reason = 'x' WHERE task_id = 't'")
    assert not transition_task(db, "p", "t", to="queued", from_="running")  # not running: untouched
    assert transition_task(
        db,
        "p",
        "t",
        to="failed",
        from_=("queued", "stopped"),
        finished=True,
        now="2026-11-04T00:00:00Z",
        stop_requested=0,
        block_reason=None,
        last_error_code="tq.run.nonzero",
    )
    row = get_task(db, "p", "t")
    assert (row["status"], row["updated_at"], row["finished_at"]) == (
        "failed",
        "2026-11-04T00:00:00Z",
        "2026-11-04T00:00:00Z",
    )
    assert (row["stop_requested"], row["block_reason"], row["last_error_code"]) == (0, None, "tq.run.nonzero")
    with pytest.raises(ValueError, match="cannot set"):
        transition_task(db, "p", "t", to="queued", from_="failed", priority=9)  # not a status-change field
    db.close()
