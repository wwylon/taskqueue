"""Post–Round-12 features: sticky preflight, snapshot allowlist, SSRF pin."""
from __future__ import annotations

from pathlib import Path

from taskqueue import errors as tqerr
from taskqueue.db import open_db, parse_task_yaml, retry_task, upsert_task
from taskqueue.web.routes_transfer import (
    _resolve_allowed_upstream_ip,
    _snapshot_path_allowed,
    _upstream_host_allowed,
)
from tests.test_queue import _project, _yaml


def test_sticky_preflight_survives_retry(tmp_path: Path) -> None:
    db = open_db(tmp_path / "q.db")
    proj = _project("p", tmp_path)
    path = _yaml(tmp_path, "p", "t1")
    upsert_task(db, parse_task_yaml(path, proj))
    db.execute(
        "UPDATE tasks SET status='failed', last_error_code=?, block_reason=? "
        "WHERE project='p' AND task_id='t1'",
        (tqerr.RUN_PREFLIGHT, "command missing: /no/such"),
    )
    db.commit()
    assert retry_task(db, "p", "t1")
    row = db.execute(
        "SELECT status, last_error_code, block_reason FROM tasks WHERE task_id='t1'"
    ).fetchone()
    assert row["status"] == "queued"
    assert row["last_error_code"] == tqerr.RUN_PREFLIGHT
    assert "missing" in (row["block_reason"] or "")
    db.close()


def test_retry_clears_non_preflight_error(tmp_path: Path) -> None:
    db = open_db(tmp_path / "q.db")
    proj = _project("p", tmp_path)
    path = _yaml(tmp_path, "p", "t2")
    upsert_task(db, parse_task_yaml(path, proj))
    db.execute(
        "UPDATE tasks SET status='failed', last_error_code=?, block_reason=? "
        "WHERE project='p' AND task_id='t2'",
        ("tq.run.nonzero", "exit 1"),
    )
    db.commit()
    assert retry_task(db, "p", "t2")
    row = db.execute(
        "SELECT last_error_code, block_reason FROM tasks WHERE task_id='t2'"
    ).fetchone()
    assert row["last_error_code"] is None
    assert row["block_reason"] is None
    db.close()


def test_snapshot_path_allowlist() -> None:
    assert _snapshot_path_allowed("data/foo.db", None)
    assert _snapshot_path_allowed("data/nested/x.sqlite", {})
    assert not _snapshot_path_allowed("secrets/pass.db", None)
    assert not _snapshot_path_allowed("../etc/passwd", None)
    assert _snapshot_path_allowed(
        "artifacts/out.db",
        {"inputs": [{"path": "artifacts/out.db", "provider": "fetch"}]},
    )
    assert not _snapshot_path_allowed(
        "artifacts/other.db",
        {"inputs": [{"path": "artifacts/out.db"}]},
    )


def test_upstream_blocks_link_local_and_pins_loopback() -> None:
    assert not _upstream_host_allowed("169.254.169.254")
    assert not _upstream_host_allowed("0.0.0.0")
    assert not _upstream_host_allowed(None)
    assert _upstream_host_allowed("127.0.0.1")
    assert _resolve_allowed_upstream_ip("127.0.0.1") == "127.0.0.1"
    assert _resolve_allowed_upstream_ip("169.254.1.1") is None
