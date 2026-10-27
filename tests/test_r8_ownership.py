"""Slice R8 -- cross-operator ownership gate on tq stop/remove/priority."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from taskqueue.cli import cmd_priority, cmd_remove, cmd_stop
from taskqueue._db_tasks import get_task
from taskqueue.db import open_db


def _seed_task(home: Path, *, started_by: str | None) -> None:
    db = open_db(home / "queue.db")
    now = datetime.now(UTC).isoformat()
    db.execute(
        "INSERT INTO tasks (project, task_id, status, command, enqueued_at, updated_at, enabled, started_by) "
        "VALUES ('p', 't1', 'queued', 'true', ?, ?, 1, ?)",
        (now, now, started_by),
    )
    db.commit()
    db.close()


def test_stop_blocked_cross_operator(tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_task(tmp_path, started_by="alice")
    monkeypatch.setenv("TQ_OPERATOR", "bob")
    rc = cmd_stop(tmp_path, "p/t1")
    assert rc == 1
    assert "alice" in capsys.readouterr().out
    # Task untouched -- stop_requested never set.
    row = get_task(open_db(tmp_path / "queue.db"), "p", "t1")
    assert (row["stop_requested"] if "stop_requested" in row.keys() else 0) in (0, None)


def test_stop_allowed_same_operator(tmp_path: Path, monkeypatch) -> None:
    _seed_task(tmp_path, started_by="alice")
    monkeypatch.setenv("TQ_OPERATOR", "alice")
    assert cmd_stop(tmp_path, "p/t1") == 0


def test_stop_allowed_no_attribution_back_compat(tmp_path: Path, monkeypatch) -> None:
    _seed_task(tmp_path, started_by=None)
    monkeypatch.setenv("TQ_OPERATOR", "bob")
    assert cmd_stop(tmp_path, "p/t1") == 0


def test_stop_force_overrides(tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_task(tmp_path, started_by="alice")
    monkeypatch.setenv("TQ_OPERATOR", "bob")
    assert cmd_stop(tmp_path, "p/t1", force=True) == 0
    assert "force" in capsys.readouterr().out.lower()


def test_remove_blocked_cross_operator(tmp_path: Path, monkeypatch) -> None:
    _seed_task(tmp_path, started_by="alice")
    monkeypatch.setenv("TQ_OPERATOR", "bob")
    assert cmd_remove(tmp_path, "p/t1") == 1
    # Still present -- not removed.
    assert get_task(open_db(tmp_path / "queue.db"), "p", "t1") is not None


def test_remove_force_overrides(tmp_path: Path, monkeypatch) -> None:
    _seed_task(tmp_path, started_by="alice")
    monkeypatch.setenv("TQ_OPERATOR", "bob")
    assert cmd_remove(tmp_path, "p/t1", force=True) == 0
    assert get_task(open_db(tmp_path / "queue.db"), "p", "t1") is None


def test_priority_blocked_cross_operator(tmp_path: Path, monkeypatch) -> None:
    _seed_task(tmp_path, started_by="alice")
    monkeypatch.setenv("TQ_OPERATOR", "bob")
    assert cmd_priority(tmp_path, "p/t1", 5) == 1
    row = get_task(open_db(tmp_path / "queue.db"), "p", "t1")
    assert row["priority"] != 5


def test_priority_force_overrides(tmp_path: Path, monkeypatch) -> None:
    _seed_task(tmp_path, started_by="alice")
    monkeypatch.setenv("TQ_OPERATOR", "bob")
    assert cmd_priority(tmp_path, "p/t1", 5, force=True) == 0
    row = get_task(open_db(tmp_path / "queue.db"), "p", "t1")
    assert row["priority"] == 5
