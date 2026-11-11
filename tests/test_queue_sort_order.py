"""sort=order ranks by Timeline forecast start order (F-181)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from tqdaemon.db import open_db
from tqdaemon.web.responses import _get_queue_response


def _ins(conn, tid: str, status: str = "queued", prio: int = 50) -> None:
    conn.execute(
        "INSERT INTO tasks (project, task_id, command, status, priority, enqueued_at, updated_at) "
        "VALUES ('p', ?, 'x', ?, ?, '2026-10-23T00:00:00', '2026-10-23T00:00:00')",
        (tid, status, prio),
    )


def test_sort_order_uses_forecast_map(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "tq.db")
    for tid in ("a", "b", "c"):
        _ins(conn, tid)
    conn.commit()
    fake = {
        ("p", "c"): {"order": 1, "start_sec": 10.0},
        ("p", "a"): {"order": 2, "start_sec": 20.0},
        ("p", "b"): {"order": 3, "start_sec": 30.0},
    }
    with patch("tqdaemon.timeline_forecast.forecast_order_map", return_value=fake):
        full = _get_queue_response(conn, sort="order", limit=50)
    ids = [t["task_id"] for t in full["tasks"]]
    assert ids == ["c", "a", "b"]
    assert full["tasks"][0]["forecast_order"] == 1
    assert full["tasks"][0]["forecast_start_sec"] == 10.0
    with patch("tqdaemon.timeline_forecast.forecast_order_map", return_value=fake):
        page = _get_queue_response(conn, sort="order", limit=1, offset=1)
    assert [t["task_id"] for t in page["tasks"]] == ["a"]
    conn.close()


def test_sort_order_empty_forecast_still_returns_rows(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "tq.db")
    _ins(conn, "x")
    _ins(conn, "y")
    conn.commit()
    with patch("tqdaemon.timeline_forecast.forecast_order_map", return_value={}):
        full = _get_queue_response(conn, sort="order", limit=50)
    assert full["total"] == 2
    assert all(t["forecast_order"] is None for t in full["tasks"])
    conn.close()
