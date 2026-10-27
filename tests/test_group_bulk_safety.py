"""Bulk group/suite/batch operations must never touch tasks outside their explicit scope.

Regression for the 2026-10-02 incident: "Restart all" on the "(ungrouped)" group matched
``grp IS NULL OR grp = ''`` across EVERY project and included completed tasks, so ~70 long-finished
tasks of one project were re-queued and re-executed (42 then failed, overwriting tracked ledgers).
Pattern follows tests/test_web_routes.py (fake handler, no HTTP server).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from taskqueue.web import make_handler
from taskqueue.db import open_db

from tests.test_web_routes import _make_request


def _setup(tmp_path: Path):
    db = open_db(tmp_path / "q.db")
    handler = make_handler(db, {}, {}, lambda: {}, tmp_path / "q.db", "127.0.0.1")
    return handler, db


def _seed(db, project, task_id, status, grp="", suite=""):
    now = datetime.now(UTC).isoformat()
    db.execute(
        "INSERT INTO tasks (project, task_id, status, command, enqueued_at, updated_at, enabled, grp, suite) "
        "VALUES (?, ?, ?, 'true', ?, ?, 1, ?, ?)",
        (project, task_id, status, now, now, grp, suite),
    )
    db.commit()


def _statuses(db, project=None):
    q = "SELECT project, task_id, status FROM tasks"
    args: tuple = ()
    if project:
        q += " WHERE project = ?"
        args = (project,)
    return {(r["project"], r["task_id"]): r["status"] for r in db.execute(q, args).fetchall()}


def _post(handler, path, body=None):
    raw = json.dumps(body).encode() if body is not None else b""
    code, data = _make_request(handler, "POST", path, raw)
    return code, (json.loads(data) if data else {})


def _two_projects(db):
    # project "mine": 3 ungrouped (2 completed, 1 failed); project "other": 2 ungrouped completed
    _seed(db, "mine", "a", "completed")
    _seed(db, "mine", "b", "completed")
    _seed(db, "mine", "c", "failed")
    _seed(db, "other", "x", "completed")
    _seed(db, "other", "y", "completed")


# --- the incident -----------------------------------------------------------------------------

def test_restart_all_ungrouped_is_project_scoped(tmp_path: Path):
    handler, db = _setup(tmp_path)
    _two_projects(db)
    code, body = _post(handler, "/api/groups//retry?completed=1&project=mine&confirm=3")
    assert code == 200, body
    assert body["retried"] == 3
    st = _statuses(db)
    assert {st[("mine", t)] for t in "abc"} == {"queued"}
    assert st[("other", "x")] == "completed" and st[("other", "y")] == "completed", (
        "restart-all leaked into another project"
    )


def test_ungrouped_bulk_without_project_is_rejected(tmp_path: Path):
    handler, db = _setup(tmp_path)
    _two_projects(db)
    before = _statuses(db)
    for path in ("/api/groups//retry?completed=1", "/api/groups//retry", "/api/groups//stop"):
        code, body = _post(handler, path)
        assert code == 400, (path, body)
        assert body["ok"] is False
    assert _statuses(db) == before


# --- restart-all needs an explicit confirmation count ------------------------------------------

def test_restart_all_requires_matching_confirm(tmp_path: Path):
    handler, db = _setup(tmp_path)
    _two_projects(db)
    before = _statuses(db)
    code, body = _post(handler, "/api/groups//retry?completed=1&project=mine")
    assert code == 409 and body["would_retry"] == 3 and body["ok"] is False
    code, body = _post(handler, "/api/groups//retry?completed=1&project=mine&confirm=99")
    assert code == 409
    assert _statuses(db) == before
    code, body = _post(handler, "/api/groups//retry?completed=1&project=mine&confirm=3")
    assert code == 200 and body["retried"] == 3


def test_dry_run_reports_counts_and_changes_nothing(tmp_path: Path):
    handler, db = _setup(tmp_path)
    _two_projects(db)
    before = _statuses(db)
    code, body = _post(handler, "/api/groups//retry?completed=1&project=mine&dry_run=1")
    assert code == 200 and body["dry_run"] is True
    assert body["would_retry"] == 3 and body["completed_included"] == 2
    code, body = _post(handler, "/api/groups//stop?project=mine&dry_run=1")
    assert code == 200 and body["would_stop"] == 0
    assert _statuses(db) == before


# --- retry-failed must not include completed; flag parsing -------------------------------------

def test_retry_failed_default_excludes_completed(tmp_path: Path):
    handler, db = _setup(tmp_path)
    _two_projects(db)
    code, body = _post(handler, "/api/groups//retry?project=mine")
    assert code == 200 and body["retried"] == 1
    st = _statuses(db, "mine")
    assert st[("mine", "c")] == "queued"
    assert st[("mine", "a")] == "completed" and st[("mine", "b")] == "completed"


def test_completed_zero_is_not_truthy(tmp_path: Path):
    handler, db = _setup(tmp_path)
    _two_projects(db)
    code, body = _post(handler, "/api/groups//retry?project=mine&completed=0")
    assert code == 200 and body["retried"] == 1
    assert _statuses(db, "mine")[("mine", "a")] == "completed"


# --- named groups, suites, batch ----------------------------------------------------------------

def test_named_group_with_slash_is_urldecoded_and_scoped(tmp_path: Path):
    handler, db = _setup(tmp_path)
    _seed(db, "mine", "g1", "failed", grp="mine/verify")
    _seed(db, "other", "g2", "failed", grp="mine/verify")
    path = f"/api/groups/{quote('mine/verify', safe='')}/retry?project=mine"
    code, body = _post(handler, path)
    assert code == 200 and body["retried"] == 1
    st = _statuses(db)
    assert st[("mine", "g1")] == "queued" and st[("other", "g2")] == "failed"


def test_empty_suite_name_is_rejected(tmp_path: Path):
    handler, db = _setup(tmp_path)
    _two_projects(db)
    before = _statuses(db)
    code, body = _post(handler, "/api/suites//retry")
    assert code == 400 and body["ok"] is False
    assert _statuses(db) == before


def test_retry_batch_default_excludes_completed(tmp_path: Path):
    handler, db = _setup(tmp_path)
    _two_projects(db)
    code, body = _post(handler, "/api/tasks/retry-batch", {"project": "mine"})
    assert code == 200
    st = _statuses(db, "mine")
    assert st[("mine", "c")] == "queued"
    assert st[("mine", "a")] == "completed", "default batch retry must not re-run completed tasks"
    code, body = _post(handler, "/api/tasks/retry-batch", {"project": "mine", "status": ["completed"]})
    assert code == 200
    assert _statuses(db, "mine")[("mine", "a")] == "queued"


# --- groups listing: ungrouped is per project so the UI can scope its buttons -------------------

def test_groups_listing_splits_ungrouped_per_project(tmp_path: Path):
    handler, db = _setup(tmp_path)
    _two_projects(db)
    code, data = _make_request(handler, "GET", "/api/groups")
    assert code == 200
    groups = json.loads(data)["groups"]
    ungrouped = [g for g in groups if g["name"] == ""]
    assert sorted(g.get("project") for g in ungrouped) == ["mine", "other"]
    by_proj = {g["project"]: g["total"] for g in ungrouped}
    assert by_proj == {"mine": 3, "other": 2}
