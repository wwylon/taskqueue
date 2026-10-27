"""Retry/add must not write SQLite behind a daemon that is running but silent."""
from __future__ import annotations

import pytest

from taskqueue import _cli_project as cp
from taskqueue.config import db_path
from taskqueue.db import open_db


@pytest.fixture
def silent_daemon(tmp_path, monkeypatch):
    open_db(db_path(tmp_path)).close()
    monkeypatch.setattr("taskqueue.master_client.via_master", lambda *a, **k: None)
    monkeypatch.setattr(cp, "_daemon_running", lambda home: 4242)
    return tmp_path


def test_retry_keys_refuses_direct_write(silent_daemon, capsys):
    assert cp.cmd_retry_keys(silent_daemon, ["p/t"]) == cp.EXIT_UNREACHABLE
    assert "refusing" in capsys.readouterr().err


def test_retry_project_refuses_direct_write(silent_daemon, capsys):
    assert cp.cmd_retry_project(silent_daemon, "p") == cp.EXIT_UNREACHABLE
    assert "refusing" in capsys.readouterr().err


def test_retry_keys_offline_still_writes(tmp_path, monkeypatch):
    open_db(db_path(tmp_path)).close()
    monkeypatch.setattr("taskqueue.master_client.via_master", lambda *a, **k: None)
    monkeypatch.setattr(cp, "_daemon_running", lambda home: None)
    assert cp.cmd_retry_keys(tmp_path, ["p/missing"]) in (0, 1)


def test_stop_project_refuses_direct_write(tmp_path, monkeypatch, capsys):
    from taskqueue import _cli_task as ct

    db = open_db(db_path(tmp_path))
    db.execute(
        "INSERT INTO tasks (project, task_id, command, enqueued_at, updated_at, status) "
        "VALUES ('p','t','x','2020-01-01','2020-01-01','running')"
    )
    db.commit()
    db.close()
    monkeypatch.setattr("taskqueue.master_client.via_master", lambda *a, **k: None)
    monkeypatch.setattr("taskqueue._cli_helpers._daemon_running", lambda home: 4242)
    called = []
    monkeypatch.setattr("taskqueue.db.request_stop_with_control", lambda *a, **k: called.append(a))
    assert ct.cmd_stop_project(tmp_path, "p") == ct.EXIT_UNREACHABLE
    assert not called


def test_retry_single_refuses_direct_write(tmp_path, monkeypatch, capsys):
    from taskqueue import _cli_task as ct

    db = open_db(db_path(tmp_path))
    db.execute(
        "INSERT INTO tasks (project, task_id, command, enqueued_at, updated_at, status) "
        "VALUES ('p','t','x','2020-01-01','2020-01-01','failed')"
    )
    db.commit()
    db.close()
    monkeypatch.setattr("taskqueue.master_client.via_master", lambda *a, **k: None)
    monkeypatch.setattr("taskqueue._cli_helpers._daemon_running", lambda home: 4242)
    assert ct.cmd_retry(tmp_path, "p/t") == ct.EXIT_UNREACHABLE
    c = open_db(db_path(tmp_path))
    assert c.execute("SELECT status FROM tasks WHERE task_id='t'").fetchone()[0] == "failed"
