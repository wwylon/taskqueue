"""C-002: idempotent submits with a client_key (HTTP /api/cli/add and MCP submit_task)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

from taskqueue import mcp_server, submit_keys
from taskqueue.config import Project, db_path
from taskqueue.db import open_db
from tests.helpers.master_worker import http_request, make_handler_env


def _yaml(task_id: str) -> str:
    return yaml.dump({"task_id": task_id, "command": sys.executable, "args": ["-c", "pass"]})


def test_claim_complete_release_conflict_and_expiry(tmp_path: Path) -> None:
    db = open_db(tmp_path / "q.db")
    d1, d2 = submit_keys.body_hash("a: 1\n"), submit_keys.body_hash("a: 2\n")
    assert submit_keys.claim(db, "p", "k", d1, now=1000.0) is None  # reserved
    with pytest.raises(submit_keys.SubmitKeyConflict, match="in progress"):
        submit_keys.claim(db, "p", "k", d1, now=1001.0)
    with pytest.raises(submit_keys.SubmitKeyConflict, match="different submission"):
        submit_keys.claim(db, "p", "k", d2, now=1001.0)
    submit_keys.complete(db, "p", "k", ["t1", "t2"], "/q/x.yaml")
    assert submit_keys.claim(db, "p", "k", d1, now=1002.0) == {"task_ids": ["t1", "t2"], "path": "/q/x.yaml"}
    assert submit_keys.claim(db, "other", "k", d2, now=1002.0) is None  # keys are per project
    # An abandoned reservation (crash mid-submit) can be taken over after PENDING_STALE_SEC.
    assert submit_keys.claim(db, "p", "stale", d1, now=1000.0) is None
    assert submit_keys.claim(db, "p", "stale", d1, now=1000.0 + submit_keys.PENDING_STALE_SEC + 1) is None
    # A failed submission releases its key; a completed one is never released.
    submit_keys.release(db, "p", "stale")
    assert submit_keys.claim(db, "p", "stale", d2, now=2000.0) is None
    submit_keys.release(db, "p", "k")
    assert submit_keys.claim(db, "p", "k", d1, now=2000.0) is not None
    # Past the TTL the key is free again.
    assert submit_keys.claim(db, "p", "k", d2, ttl_sec=60, now=1002.0 + 61) is None
    assert submit_keys.key_error("x" * 129) and submit_keys.key_error("") and submit_keys.key_error("a\nb")
    assert submit_keys.key_error("agent-run-42/step-3") is None


def _env(tmp_path: Path):
    root = tmp_path / "proj"
    qdir = root / "experiments" / "operator_queue"
    qdir.mkdir(parents=True)
    proj = Project(name="p", root=root, queue_dir=qdir)
    handler, db, _ = make_handler_env(tmp_path, projects={"p": proj})
    return handler, db, qdir


def test_http_retry_with_same_key_returns_first_result_without_a_second_file(tmp_path: Path) -> None:
    H, db, qdir = _env(tmp_path)
    body = {"project": "p", "yaml_text": _yaml("t1"), "client_key": "agent-7"}
    code1, first = http_request(H, "POST", "/api/cli/add", body)
    code2, again = http_request(H, "POST", "/api/cli/add", body)
    assert code1 == 200 and first["ok"] and "deduplicated" not in first
    assert code2 == 200 and again["task_ids"] == ["t1"] and again["deduplicated"] is True
    assert again["path"] == first["path"]
    assert len(list(qdir.glob("*.yaml"))) == 1  # without the key every submit gets a new file

    code, clash = http_request(H, "POST", "/api/cli/add", {**body, "yaml_text": _yaml("t2")})
    assert code == 409 and not clash["ok"] and "different submission" in clash["error"]
    code, bad = http_request(H, "POST", "/api/cli/add", {**body, "client_key": "x" * 200})
    assert code == 400 and "client_key" in bad["error"]
    db.close()


def test_http_failed_submit_releases_its_key(tmp_path: Path) -> None:
    H, db, qdir = _env(tmp_path)
    code, err = http_request(
        H, "POST", "/api/cli/add", {"project": "p", "yaml_text": "just: a mapping\n", "client_key": "k"}
    )
    assert code == 400 and not err["ok"]
    code, ok = http_request(H, "POST", "/api/cli/add", {"project": "p", "yaml_text": _yaml("fixed"), "client_key": "k"})
    assert code == 200 and ok["task_ids"] == ["fixed"]  # the corrected retry may reuse the key
    db.close()


def test_mcp_offline_submit_honours_client_key(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    open_db(db_path(home)).close()
    root = tmp_path / "proj"
    qdir = root / "experiments" / "operator_queue"
    root.mkdir()
    proj = Project(name="p", root=root, queue_dir=qdir)
    monkeypatch.setattr("taskqueue.config.load_projects", lambda h: {"p": proj})
    monkeypatch.setattr("taskqueue.master_client.via_master", lambda *a, **k: None)
    args = {"project": "p", "yaml_text": "task_id: t1\ncommand: echo hi\n", "client_key": "run-1"}
    r1 = mcp_server._submit_task_nowait(home, args)
    r2 = mcp_server._submit_task_nowait(home, args)
    assert r1["ok"] and r2["ok"] and r2["deduplicated"] is True and r2["task_ids"] == ["t1"]
    assert len(list(qdir.glob("*.yaml"))) == 1


def test_mcp_online_passes_client_key_and_reports_dedup(tmp_path: Path, monkeypatch) -> None:
    sent = {}

    def fake(home, method, path, body=None, **kw):
        sent.update(body)
        return {"ok": True, "task_ids": ["t1"], "deduplicated": True}

    monkeypatch.setattr("taskqueue.master_client.via_master", fake)
    out = mcp_server._submit_task_nowait(tmp_path, {"project": "p", "yaml_text": "task_id: t1\n", "client_key": "k9"})
    assert sent["client_key"] == "k9" and out == {"ok": True, "task_ids": ["t1"], "errors": [], "deduplicated": True}
