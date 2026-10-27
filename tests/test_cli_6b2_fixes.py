"""6b.2 CLI fixes: no SQLite fallback when the master is alive, strict enabled=, masked auth_token."""

from __future__ import annotations

from pathlib import Path

from taskqueue import master_client
from taskqueue.db import open_db


def test_via_master_alive_but_unanswering_is_error_not_none(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("TQ_MASTER", raising=False)
    monkeypatch.setattr("taskqueue._cli_helpers._daemon_running", lambda home: 123)
    monkeypatch.setattr(master_client, "api_request", lambda *a, **k: None)
    resp = master_client.via_master(tmp_path, "POST", "/api/stop/p/t")
    assert resp is not None and resp["ok"] is False and resp["unreachable"] is True


def test_via_master_no_daemon_still_none(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("TQ_MASTER", raising=False)
    monkeypatch.setattr("taskqueue._cli_helpers._daemon_running", lambda home: None)
    assert master_client.via_master(tmp_path, "POST", "/api/stop/p/t") is None


def test_cmd_patch_rejects_bad_enabled(tmp_path: Path, capsys) -> None:
    from taskqueue._cli_task import cmd_patch

    assert cmd_patch(tmp_path, "p/t", ["enabled=ture"]) == 1
    assert "enabled must be true/false" in capsys.readouterr().out


def test_config_set_masks_auth_token(tmp_path: Path, capsys) -> None:
    from taskqueue._cli_admin import cmd_config_set

    open_db(tmp_path / "queue.db").close()
    assert cmd_config_set(tmp_path, ["auth_token=supersecretvalue123"]) == 0
    out = capsys.readouterr().out
    assert "supersecretvalue123" not in out
    assert "***" in out


def test_config_set_preserves_spaces_in_value(tmp_path) -> None:
    from taskqueue._cli_admin import cmd_config_set
    from taskqueue.db import get_config, open_db
    from taskqueue.config import db_path

    assert cmd_config_set(tmp_path, ["retention_projects=a b"]) == 0
    conn = open_db(db_path(tmp_path))
    try:
        assert get_config(conn)["retention_projects"] == "a b"
    finally:
        conn.close()


def test_config_set_spaced_equals_still_joined(tmp_path) -> None:
    from taskqueue._cli_admin import cmd_config_set

    assert cmd_config_set(tmp_path, ["retention_projects", "=", "x"]) == 0


def test_iter_project_sources_skips_venv(tmp_path) -> None:
    from taskqueue._cli_admin import _iter_project_sources

    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "v.py").write_text("x", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "b.cpp").write_text("x", encoding="utf-8")
    names = sorted(p.name for p in _iter_project_sources(tmp_path))
    assert names == ["a.py", "b.cpp"]


def test_cmd_patch_offline_echoes_old_new_and_fails_when_db_unchanged(tmp_path: Path, capsys, monkeypatch) -> None:
    from taskqueue import _cli_task
    from taskqueue.config import load_projects
    from taskqueue.db import get_task, parse_task_yaml, upsert_task

    root = tmp_path / "proj"
    q = root / "experiments" / "operator_queue"
    q.mkdir(parents=True)
    (tmp_path / "projects.yaml").write_text(f"projects:\n  p:\n    root: {root.as_posix()}\n", encoding="utf-8")
    y = q / "t.yaml"
    y.write_text("task_id: t\ncommand: python\npriority: 40\n", encoding="utf-8")
    db = open_db(tmp_path / "queue.db")
    upsert_task(db, parse_task_yaml(y, load_projects(tmp_path)["p"]))
    db.close()
    monkeypatch.setattr("taskqueue.master_client.via_master", lambda *a, **k: None)

    assert _cli_task.cmd_patch(tmp_path, "p/t", ["enabled=0", "priority=70"]) == 0
    out = capsys.readouterr().out
    assert "enabled: 1 -> 0" in out and "priority: 40 -> 70" in out
    db = open_db(tmp_path / "queue.db")
    row = get_task(db, "p", "t")
    assert row["enabled"] == 0 and row["priority"] == 70
    db.close()

    # A patch that does not reach the DB (here: upsert neutralised) is reported as an error.
    monkeypatch.setattr("taskqueue.db.upsert_task", lambda *a, **k: None)
    assert _cli_task.cmd_patch(tmp_path, "p/t", ["priority=90"]) == 1
    assert "did not change" in capsys.readouterr().err
