"""Last run's progress is returned and shown for failed / interrupted / partially-run queued tasks."""
from __future__ import annotations

from pathlib import Path

import pytest

from taskqueue import config as tqconfig
from taskqueue._fmt import shown_progress
from taskqueue.db import open_db
from taskqueue.web.responses import _get_queue_response
from tests.test_health import _task


@pytest.mark.parametrize(
    "status,pct,expected",
    [
        ("running", 40.0, 40.0),
        ("failed", 73.3, 73.3),
        ("failed", 100.0, 100.0),
        ("interrupted", 12.0, 12.0),
        ("queued", 55.0, 55.0),     # resume point of a partial run
        ("queued", 100.0, None),    # leftover from an earlier finished run
        ("queued", 0.0, None),
        ("completed", 84.0, None),  # done means done
        ("failed", None, None),
    ],
)
def test_shown_progress_rule(status, pct, expected) -> None:
    assert shown_progress(status, pct) == expected


def test_queue_response_carries_last_run_progress(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TASKQUEUE_HOME", str(tmp_path))
    conn = open_db(tqconfig.db_path(tmp_path))
    try:
        _task(conn, "boom", "failed")
        conn.execute("UPDATE runs SET progress_pct = 73.3")
        conn.commit()
        rows = {t["task_id"]: t for t in _get_queue_response(conn)["tasks"]}
        assert rows["boom"]["progress_pct"] == 73.3
    finally:
        conn.close()
