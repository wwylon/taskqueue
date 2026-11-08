"""Owner-facing stop_summary helpers (F-210 stop-reason UX)."""

from __future__ import annotations

from pathlib import Path

from taskqueue._db_runs import insert_run, set_run_stop_reason
from taskqueue.db import open_db
from taskqueue.stop_summary import attach_stop_fields, format_stop_summary, label_stop_reason
from taskqueue.why import explain_task
from tests.helpers.master_worker import seed_queued_task


def test_format_stop_summary_tq_and_human() -> None:
    assert "TaskQueue" in (
        format_stop_summary(stopped_by="tq", stop_reason="ram_hard", last_error_code="tq.stop.ram") or ""
    )
    assert "gate:lagging" in (format_stop_summary(stopped_by="user", stop_reason="gate:lagging") or "")
    assert label_stop_reason("wall_clock_cap") == "wall-clock cap"
    assert format_stop_summary(status="completed") is None


def test_why_includes_stop_summary(tmp_path: Path) -> None:
    db = open_db(tmp_path / "queue.db")
    seed_queued_task(db, project="p", task_id="t", dispatch="local")
    run_id = insert_run(db, "p", "t", str(tmp_path / "t.log"))
    set_run_stop_reason(db, run_id, "ram_hard")
    db.execute(
        "UPDATE runs SET status='stopped', stopped_by='tq', last_error_code='tq.stop.ram', "
        "finished_at=started_at WHERE run_id=?",
        (run_id,),
    )
    db.execute("UPDATE tasks SET status='stopped', last_error_code='tq.stop.ram' WHERE project='p' AND task_id='t'")
    db.commit()
    info = explain_task(db, "p", "t")
    assert info["ok"]
    assert info.get("stop_reason") == "ram_hard"
    assert any("TaskQueue" in r for r in info["reasons"])
    attached = attach_stop_fields(db, "p", "t")
    assert attached.get("stop_summary")
    db.close()


def test_every_tq_stop_reason_has_a_label():
    """Wave 4: one policy set (stop_reasons) — every tq stop reason must be explained to owners."""
    from taskqueue.stop_reasons import TQ_STOP_REASONS, stopped_by_for
    from taskqueue.stop_summary import _STOP_REASON_LABELS

    assert set(_STOP_REASON_LABELS) >= TQ_STOP_REASONS
    assert stopped_by_for("scavenger_preempt") == "tq" and stopped_by_for("stop_requested") is None
