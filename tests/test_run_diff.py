from __future__ import annotations

from taskqueue.db import finish_run, insert_run, open_db
from taskqueue.run_diff import diff_runs, render_diff
from tests.helpers.master_worker import seed_queued_task


def _runs(tmp_path):
    db = open_db(tmp_path / "q.db")
    seed_queued_task(db, project="p", task_id="t", dispatch="local")
    a = insert_run(db, "p", "t", "l")
    b = insert_run(db, "p", "t", "l")
    db.execute("UPDATE runs SET started_at='2026-01-01T00:00:00+00:00', finished_at='2026-01-01T00:01:00+00:00',"
               " status='completed', exit_code=0, peak_ram_gib=4.0 WHERE run_id=?", (a,))
    db.execute("UPDATE runs SET started_at='2026-01-02T00:00:00+00:00', finished_at='2026-01-02T00:03:00+00:00',"
               " status='failed', exit_code=1, peak_ram_gib=6.0 WHERE run_id=?", (b,))
    db.commit()
    return db, a, b


def test_diff_reports_changed_fields_and_deltas(tmp_path):
    db, a, b = _runs(tmp_path)
    d = diff_runs(db, a, b)
    assert d["a"]["run_id"] == a and d["b"]["run_id"] == b
    ch = {c["field"]: c for c in d["changes"]}
    assert ch["status"]["a"] == "completed" and ch["status"]["b"] == "failed"
    assert ch["exit_code"]["delta"] == 1
    assert ch["duration_sec"]["a"] == 60 and ch["duration_sec"]["b"] == 180 and ch["duration_sec"]["delta"] == 120
    assert ch["peak_ram_gib"]["delta"] == 2.0
    assert "run_id" not in ch and "log_path" not in ch
    text = render_diff(d)
    assert "status" in text and "completed" in text and "failed" in text


def test_diff_missing_run_raises(tmp_path):
    import pytest

    db, a, _ = _runs(tmp_path)
    with pytest.raises(LookupError):
        diff_runs(db, a, 99999)
