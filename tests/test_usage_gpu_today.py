"""/api/usage reports GPU hours used today (UTC) against the project's daily budget."""
from taskqueue.db import open_db
from taskqueue.web.responses import _get_usage_response


def test_usage_gpu_today_with_budget(tmp_path, monkeypatch):
    conn = open_db(tmp_path / "queue.db")
    monkeypatch.setattr(
        "taskqueue.db.get_all_project_limits",
        lambda c: {"p": {"max_gpu_hours_day": 2.0}, "q": {"max_gpu_hours_day": 0.0}},
    )
    monkeypatch.setattr("taskqueue.db.gpu_hours_today", lambda c, p: 2.5 if p == "p" else 0.0)
    out = _get_usage_response(conn, days=7)
    assert out["gpu_today"] == {
        "p": {"used_hours": 2.5, "limit_hours": 2.0, "remaining_hours": 0.0}
    }
    only = _get_usage_response(conn, project="zzz", days=7)
    assert only["gpu_today"] == {}
    conn.close()


def test_usage_gpu_today_no_budget_uses_none_remaining(tmp_path, monkeypatch):
    conn = open_db(tmp_path / "queue.db")
    monkeypatch.setattr("taskqueue.db.get_all_project_limits", lambda c: {})
    conn.execute(
        "INSERT INTO usage_snapshot (ts, project, operator, cpu_seconds, gpu_seconds) "
        "VALUES (strftime('%s','now'), 'p', 'o', 1, 1)"
    )
    monkeypatch.setattr("taskqueue.db.gpu_hours_today", lambda c, p: 1.25)
    out = _get_usage_response(conn, days=7)
    assert out["gpu_today"]["p"]["remaining_hours"] is None
    assert out["gpu_today"]["p"]["limit_hours"] == 0.0
    conn.close()
