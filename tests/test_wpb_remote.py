"""WP-B: run ownership, relative requires_paths, lease renew gated by worker."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from taskqueue._db_tasks import check_ownership, request_stop
from taskqueue._db_runs import get_run_owned, renew_run_lease_for_worker
from taskqueue.db import open_db
from taskqueue.web import relativize_requires_paths


def test_relativize_requires_paths(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    abs_data = root / "data" / "x.bin"
    abs_data.parent.mkdir(parents=True)
    abs_data.write_bytes(b"x")
    got = relativize_requires_paths([str(abs_data), "rel/y"], root)
    assert got[0] == "data/x.bin"
    assert got[1] == "rel/y"


def test_renew_and_get_run_owned(tmp_path: Path) -> None:
    db = open_db(tmp_path / "q.db")
    past = (datetime.now(UTC) - timedelta(seconds=10)).isoformat()
    cur = db.execute(
        "INSERT INTO runs (project, task_id, started_at, status, log_path, worker_id, lease_expires_at) "
        "VALUES ('p','t', ?, 'running', 'x.log', 'w1', ?)",
        (past, past),
    )
    run_id = cur.lastrowid
    db.commit()
    assert renew_run_lease_for_worker(db, run_id, "w1") is True
    assert renew_run_lease_for_worker(db, run_id, "w2") is False
    assert get_run_owned(db, run_id, "w1") is not None
    assert get_run_owned(db, run_id, "w2") is None


def test_check_ownership() -> None:
    # No attribution at all -> never blocked (back-compat: pre-existing
    # tasks / tasks started without TQ_OPERATOR set keep working exactly
    # as before this slice).
    assert check_ownership(None, None) is None
    assert check_ownership(None, "bob") is None
    # Same operator -> never blocked.
    assert check_ownership("alice", "alice") is None
    # Different operator, no force -> blocked.
    assert check_ownership("alice", "bob") is not None
    assert "alice" in check_ownership("alice", "bob")
    # Caller has no TQ_OPERATOR set at all, task is owned by someone -> blocked.
    assert check_ownership("alice", None) is not None
    # force always overrides.
    assert check_ownership("alice", "bob", force=True) is None
    assert check_ownership("alice", None, force=True) is None


def test_stop_requested_readable_for_owned_run(tmp_path: Path) -> None:
    db = open_db(tmp_path / "q.db")
    now = datetime.now(UTC).isoformat()
    db.execute(
        "INSERT INTO tasks (project, task_id, status, command, enqueued_at, updated_at, enabled) "
        "VALUES ('p','t','running','true', ?, ?, 1)",
        (now, now),
    )
    cur = db.execute(
        "INSERT INTO runs (project, task_id, started_at, status, log_path, worker_id, lease_expires_at) "
        "VALUES ('p','t', ?, 'running', 'x.log', 'w1', ?)",
        (now, now),
    )
    run_id = cur.lastrowid
    db.commit()
    request_stop(db, "p", "t")
    assert renew_run_lease_for_worker(db, run_id, "w1") is True
    row = db.execute(
        "SELECT t.stop_requested FROM runs r "
        "JOIN tasks t ON t.project = r.project AND t.task_id = r.task_id "
        "WHERE r.run_id = ?",
        (run_id,),
    ).fetchone()
    assert row["stop_requested"] == 1
