"""Round 12 Wave D: durable webhook enqueue, MCP wait clamp, shared resources parse."""
from __future__ import annotations

import queue
from pathlib import Path
from unittest import mock

import pytest

from taskqueue import config as tqconfig
from taskqueue import mcp_server, notify
from taskqueue.db import open_db
from taskqueue.rows import resources_dict_from_mapping


def test_f145_queue_full_dead_letters(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    open_db(tqconfig.db_path(home)).close()
    with mock.patch.object(notify, "_ensure_webhook_worker"), \
         mock.patch.object(notify, "_WEBHOOK_QUEUE") as q, \
         mock.patch.object(notify, "validate_notify_url", return_value="http://127.0.0.1:9/hook"), \
         mock.patch.object(notify, "_send_toast"), \
         mock.patch.object(notify, "append_notify_event"):
        q.put_nowait.side_effect = queue.Full()
        notify.emit_task_notify(
            home,
            project="p",
            task_id="t",
            status="completed",
            run_id=1,
            exit_code=0,
            last_error_code=None,
            notify_url="http://127.0.0.1:9/hook",
            started_by=None,
            post=True,
            cfg={"toast_notifications": False},
        )
    db = open_db(tqconfig.db_path(home))
    try:
        rows = notify.list_dead_letters(db, limit=10)
    finally:
        db.close()
    assert len(rows) == 1
    assert "queue full" in (rows[0].get("error") or "")


def test_mcp_wait_env_cannot_exceed_100(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TQ_MCP_WAIT_MAX_SEC", "999")
    assert mcp_server._mcp_wait_max_sec() == 100.0
    monkeypatch.setenv("TQ_MCP_WAIT_MAX_SEC", "30")
    assert mcp_server._mcp_wait_max_sec() == 30.0
    monkeypatch.delenv("TQ_MCP_WAIT_MAX_SEC", raising=False)
    assert mcp_server._mcp_wait_max_sec() == 100.0


def test_resources_dict_from_mapping_ceil_cpu() -> None:
    d = resources_dict_from_mapping({"cpu_cores": 1.2, "ram_gib": 2, "locks": ["a"]})
    assert d["cpu_cores"] == 2
    assert d["ram_gib"] == 2.0
    assert d["locks"] == ("a",)
