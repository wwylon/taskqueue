"""POST /api/cli/sync reuses the daemon's mtime-cached project sync (scales to thousands of YAMLs)."""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

from taskqueue.config import Project
from taskqueue.yaml_sync import clear_yaml_mtime_cache
from tests.helpers.master_worker import http_request, make_handler_env


def _project(tmp_path: Path, name: str, n: int) -> Project:
    root = tmp_path / name
    qdir = root / "experiments" / "operator_queue"
    qdir.mkdir(parents=True)
    for i in range(n):
        (qdir / f"t{i}.yaml").write_text(
            yaml.dump({"task_id": f"t{i}", "command": sys.executable, "args": ["-c", "pass"]}),
            encoding="utf-8",
        )
    return Project(name=name, root=root, queue_dir=qdir)


def _spy_parses(monkeypatch) -> list[str]:
    import taskqueue.yaml_sync as ys

    real = ys.parse_task_yaml_many
    seen: list[str] = []

    def _spy(path, *a, **k):
        seen.append(Path(path).name)
        return real(path, *a, **k)

    monkeypatch.setattr(ys, "parse_task_yaml_many", _spy)
    return seen


def test_second_sync_parses_nothing(tmp_path: Path, monkeypatch) -> None:
    clear_yaml_mtime_cache()
    a = _project(tmp_path, "a", 5)
    HandlerClass, db, _ = make_handler_env(tmp_path, projects={"a": a})
    seen = _spy_parses(monkeypatch)

    code, data = http_request(HandlerClass, "POST", "/api/cli/sync")
    assert code == 200 and data["ok"] is True
    # F-119: total/projects are upserted counts for this call, not COUNT(*) of all tasks.
    assert data["projects"] == {"a": 5} and data["total"] == 5
    assert data.get("deleted", 0) == 0
    assert len(seen) == 5

    seen.clear()
    code, data = http_request(HandlerClass, "POST", "/api/cli/sync")
    assert code == 200 and data["total"] == 0  # mtime cache → no upserts
    assert seen == []
    assert db.execute("SELECT COUNT(*) FROM tasks WHERE project='a'").fetchone()[0] == 5

    # An edited file is re-read.
    p = a.queue_dir / "t2.yaml"
    p.write_text(
        yaml.dump({"task_id": "t2", "command": sys.executable, "args": ["-c", "pass"], "priority": 7}),
        encoding="utf-8",
    )
    import os
    os.utime(p, ns=(p.stat().st_atime_ns, p.stat().st_mtime_ns + 5_000_000_000))
    http_request(HandlerClass, "POST", "/api/cli/sync")
    assert seen == ["t2.yaml"]
    assert int(db.execute("SELECT priority FROM tasks WHERE task_id='t2'").fetchone()["priority"]) == 7
    db.close()


def test_force_reparses_everything(tmp_path: Path, monkeypatch) -> None:
    clear_yaml_mtime_cache()
    a = _project(tmp_path, "a", 3)
    HandlerClass, db, _ = make_handler_env(tmp_path, projects={"a": a})
    seen = _spy_parses(monkeypatch)
    http_request(HandlerClass, "POST", "/api/cli/sync")
    seen.clear()
    code, data = http_request(HandlerClass, "POST", "/api/cli/sync", {"force": True})
    assert code == 200 and data["ok"] is True
    assert len(seen) == 3
    db.close()


def test_sync_single_project_and_unknown(tmp_path: Path, monkeypatch) -> None:
    clear_yaml_mtime_cache()
    a = _project(tmp_path, "a", 2)
    b = _project(tmp_path, "b", 3)
    HandlerClass, db, _ = make_handler_env(tmp_path, projects={"a": a, "b": b})

    code, data = http_request(HandlerClass, "POST", "/api/cli/sync", {"projects": ["b"]})
    assert code == 200 and data["projects"] == {"b": 3} and data["total"] == 3
    rows = {r["project"] for r in db.execute("SELECT DISTINCT project FROM tasks").fetchall()}
    assert rows == {"b"}

    code, data = http_request(HandlerClass, "POST", "/api/cli/sync", {"projects": ["nope"]})
    assert code == 404 and data["ok"] is False
    db.close()


def test_cli_sync_passes_projects_and_force(tmp_path: Path, monkeypatch) -> None:
    from taskqueue import _cli_project

    seen: dict = {}

    def _fake(home, method, path, *, body=None, timeout=8.0):
        seen.update(path=path, body=body)
        return {"ok": True, "total": 3, "projects": {"b": 3}}

    monkeypatch.setattr("taskqueue.master_client.via_master", _fake)
    monkeypatch.setattr("taskqueue._cli_helpers.warn_enabled_terminal_summary", lambda db: None)
    monkeypatch.setattr("taskqueue.db.open_db", lambda *a, **k: type("D", (), {"close": lambda s: None})())
    monkeypatch.setattr(_cli_project, "print_wait_hint", lambda: None)
    assert _cli_project.cmd_sync(tmp_path, projects=["b"], force=True) == 0
    assert seen == {"path": "/api/cli/sync", "body": {"projects": ["b"], "force": True}}


def test_cli_sync_accepts_project_flag(tmp_path: Path, monkeypatch) -> None:
    from taskqueue import cli

    got: dict = {}
    monkeypatch.setattr(cli, "cmd_sync", lambda home, **k: got.update(k) or 0)
    assert cli._cmd_sync(tmp_path, ["--project", "b", "--force"]) == 0
    assert got["projects"] == ["b"] and got["force"] is True
