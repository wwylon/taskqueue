"""MCP retry validates --set overrides with the same check as the web route."""
from __future__ import annotations

from pathlib import Path

import pytest

from taskqueue import config as tqconfig
from taskqueue import master_client, mcp_server
from taskqueue.db import open_db


@pytest.fixture()
def home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("TASKQUEUE_HOME", str(tmp_path))
    monkeypatch.setattr(master_client, "via_master", lambda *a, **k: None)
    c = open_db(tqconfig.db_path(tmp_path))
    c.execute(
        "INSERT INTO tasks (project, task_id, command, args, overridable_args, status, "
        "enabled, enqueued_at, updated_at) "
        "VALUES ('p','t','x','[\"--a=1\"]','[\"--epochs\"]','failed',1,'n','n')"
    )
    c.commit()
    c.close()
    return tmp_path


def test_non_overridable_key_rejected(home: Path) -> None:
    res = mcp_server._enqueue_task(home, {"project": "p", "task_id": "t", "set": {"--evil": 1}})
    assert not res["ok"]
    assert "not in overridable_args" in res["message"]
    assert res["status"] == "failed"


def test_overridable_key_accepted(home: Path) -> None:
    res = mcp_server._enqueue_task(home, {"project": "p", "task_id": "t", "set": {"--epochs": 0}})
    assert res["ok"], res
