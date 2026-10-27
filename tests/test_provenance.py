"""v22 task provenance: submitted_via / submitted_by / client_info."""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

from taskqueue.config import Project
from taskqueue.db import SCHEMA_VERSION, open_db
from taskqueue._db_tasks import stamp_provenance
from tests.helpers.master_worker import http_request, make_handler_env


def _yaml(task_id: str) -> str:
    return yaml.dump({"task_id": task_id, "command": sys.executable, "args": ["-c", "pass"]})


def _row(db, task_id: str):
    return db.execute(
        "SELECT submitted_via, submitted_by, client_info FROM tasks WHERE task_id = ?", (task_id,)
    ).fetchone()


def test_schema_has_provenance_columns(tmp_path: Path) -> None:
    db = open_db(tmp_path / "q.db")
    cols = {r[1] for r in db.execute("PRAGMA table_info(tasks)")}
    assert {"submitted_via", "submitted_by", "client_info"} <= cols
    assert SCHEMA_VERSION >= 22
    db.close()


def test_stamp_is_set_once_and_yaml_is_provisional(tmp_path: Path) -> None:
    db = open_db(tmp_path / "q.db")
    db.execute(
        "INSERT INTO tasks (project, task_id, command, status, enqueued_at, updated_at) "
        "VALUES ('p','t','x','queued','n','n')"
    )
    stamp_provenance(db, "p", ["t"], "yaml")  # daemon scan got there first
    assert _row(db, "t")["submitted_via"] == "yaml"
    stamp_provenance(db, "p", ["t"], "mcp", "ada", "mcp/claude-code 2.0")  # explicit replaces yaml
    r = _row(db, "t")
    assert (r["submitted_via"], r["submitted_by"], r["client_info"]) == ("mcp", "ada", "mcp/claude-code 2.0")
    stamp_provenance(db, "p", ["t"], "cli", "bob")  # real stamp is never overwritten
    stamp_provenance(db, "p", ["t"], "yaml")
    assert _row(db, "t")["submitted_by"] == "ada"
    stamp_provenance(db, "p", ["missing"], "bogus-channel")  # no row: no error
    db.close()


def test_cli_add_route_stamps_and_audits(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    qdir = root / "experiments" / "operator_queue"
    qdir.mkdir(parents=True)
    proj = Project(name="p", root=root, queue_dir=qdir)
    HandlerClass, db, _ = make_handler_env(tmp_path, projects={"p": proj})
    code, data = http_request(
        HandlerClass, "POST", "/api/cli/add",
        {"project": "p", "filename": "a.yaml", "yaml_text": _yaml("a")},
        extra_headers={"X-TQ-Via": "mcp", "X-TQ-Operator": "ada", "X-TQ-Client": "mcp/cursor 1.0"},
    )
    assert code == 200 and data["ok"]
    r = _row(db, "a")
    assert (r["submitted_via"], r["submitted_by"], r["client_info"]) == ("mcp", "ada", "mcp/cursor 1.0")
    audit = db.execute("SELECT actor, action, project, target FROM operator_audit").fetchone()
    assert tuple(audit) == ("ada", "task_add", "p", "a")
    # no headers at all -> generic api caller
    http_request(HandlerClass, "POST", "/api/cli/add",
                 {"project": "p", "filename": "b.yaml", "yaml_text": _yaml("b")})
    assert _row(db, "b")["submitted_via"] == "api"
    db.close()


def test_identity_headers() -> None:
    from taskqueue import master_client as mc

    mc.set_identity("mcp", "mcp/cursor 1.0")
    try:
        h = mc.identity_headers()
        assert h["X-TQ-Via"] == "mcp" and h["X-TQ-Client"] == "mcp/cursor 1.0"
    finally:
        mc.set_identity("cli", "")
