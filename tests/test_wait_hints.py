"""Next-step ``tq wait`` hints after tasks are created or re-queued (CLI stderr + MCP ``next``)."""
from __future__ import annotations

from pathlib import Path

import pytest

from taskqueue import config as tqconfig
from taskqueue import master_client, mcp_server
from taskqueue._cli_project import cmd_retry_keys
from taskqueue.db import open_db
from taskqueue.waiting import print_wait_hint, wait_command, wait_hint
from tests.test_waiting import _mk


@pytest.fixture()
def home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("TASKQUEUE_HOME", str(tmp_path))
    monkeypatch.delenv("TQ_NO_HINTS", raising=False)
    open_db(tqconfig.db_path(tmp_path)).close()
    return tmp_path


def test_wait_command_shapes() -> None:
    assert wait_command(["p/a"]) == "tq wait p/a --timeout 3600"
    assert wait_command(["p/a", "p/b"], project="p") == "tq wait p/a p/b --timeout 3600"
    many = [f"p/t{i}" for i in range(6)]
    assert wait_command(many, project="p") == "tq wait --project p --timeout 3600"
    assert wait_command(project="p") == "tq wait --project p --timeout 3600"
    assert "<project>/<task_id>" in wait_command()
    assert wait_hint(["p/a"]).startswith("Next: tq wait p/a")


def test_print_wait_hint_goes_to_stderr_and_can_be_silenced(capsys, monkeypatch) -> None:
    monkeypatch.delenv("TQ_NO_HINTS", raising=False)
    print_wait_hint(["p/a"])
    out = capsys.readouterr()
    assert out.out == "" and "tq wait p/a" in out.err

    monkeypatch.setenv("TQ_NO_HINTS", "1")
    print_wait_hint(["p/a"])
    assert capsys.readouterr().err == ""


def test_cli_retry_keys_prints_hint(home: Path, capsys, monkeypatch) -> None:
    monkeypatch.setattr(master_client, "via_master", lambda *a, **k: None)
    c = open_db(tqconfig.db_path(home))
    _mk(c, "p", "a", "failed")
    c.close()
    assert cmd_retry_keys(home, ["p/a"]) == 0
    assert "Next: tq wait p/a --timeout 3600" in capsys.readouterr().err


def test_mcp_retry_returns_next(home: Path, monkeypatch) -> None:
    monkeypatch.setattr(master_client, "via_master", lambda *a, **k: None)
    c = open_db(tqconfig.db_path(home))
    _mk(c, "p", "a", "failed")
    c.close()
    res = mcp_server._enqueue_task(home, {"project": "p", "task_id": "a"})
    assert res["ok"]
    assert res["next"]["tool"] == "wait_for_task"
    assert res["next"]["arguments"]["tasks"] == ["p/a"]
    assert res["next"]["cli"] == "tq wait p/a --timeout 3600"

    missing = mcp_server._enqueue_task(home, {"project": "p", "task_id": "nope"})
    assert "next" not in missing


def test_mcp_submit_returns_next_unless_waiting(home: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        master_client, "via_master", lambda *a, **k: {"ok": True, "task_ids": ["x", "y"]}
    )
    res = mcp_server._submit_task(home, {"project": "p", "yaml_text": "task_id: x\n"})
    assert res["next"]["arguments"]["tasks"] == ["p/x", "p/y"]

    monkeypatch.setattr(mcp_server, "_wait_for_task", lambda _h, _a: {"done": True})
    res = mcp_server._submit_task(home, {"project": "p", "yaml_text": "task_id: x\n", "wait": True})
    assert "next" not in res and res["wait"] == {"done": True}
