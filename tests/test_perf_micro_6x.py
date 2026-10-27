"""Perf/memory micro-fixes: fairshare naive timestamps, new indexes, BFS deque."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from taskqueue.db import open_db
from taskqueue.resources import sort_queued_fairshare


def test_fairshare_age_bonus_handles_naive_timestamp() -> None:
    old = (datetime.now(UTC) - timedelta(hours=2)).replace(tzinfo=None).isoformat()
    new = datetime.now(UTC).isoformat()
    rows = [
        {"priority": 50, "project": "a", "enqueued_at": new},
        {"priority": 50, "project": "b", "enqueued_at": old},  # naive: used to raise -> bonus 0
    ]
    out = sort_queued_fairshare(rows, {}, {})
    assert out[0]["project"] == "b"


def test_new_indexes_exist(tmp_path: Path) -> None:
    db = open_db(tmp_path / "q.db")
    names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_resource_history_ts" in names
    assert "idx_tasks_yaml_source" in names
    db.close()
