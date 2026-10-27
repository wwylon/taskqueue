"""Broken dependency detection, tq health/list multi-status, deps API health."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

from taskqueue._cli_admin import cmd_attention, cmd_health
from taskqueue._cli_task import cmd_list
from taskqueue.db import open_db
from taskqueue.mcp_server import TOOLS, _call_tool
from taskqueue.queries import build_task_health, find_broken_deps
from taskqueue.web import _get_deps_response, _get_queue_response


def _seed(
    conn,
    project: str,
    task_id: str,
    *,
    status: str = "queued",
    depends_on: list[str] | None = None,
    enabled: int = 1,
) -> None:
    now = "2026-01-01T00:00:00+00:00"
    deps = json.dumps(depends_on or [])
    conn.execute(
        "INSERT INTO tasks (project, task_id, status, command, enabled, depends_on, "
        "enqueued_at, updated_at) VALUES (?, ?, ?, 'true', ?, ?, ?, ?)",
        (project, task_id, status, enabled, deps, now, now),
    )
    conn.commit()


def test_find_broken_deps_missing_same_and_cross_project(tmp_path: Path) -> None:
    db = open_db(tmp_path / "q.db")
    _seed(db, "p", "a", status="completed")
    _seed(db, "p", "b", depends_on=["a"])  # healthy
    _seed(db, "p", "c", depends_on=["nope"])  # missing same-project
    _seed(db, "other", "x", status="completed")
    _seed(db, "p", "d", depends_on=["other/x"])  # healthy cross
    _seed(db, "p", "e", depends_on=["other/missing"])  # missing cross
    broken = find_broken_deps(db, project="p")
    by_id = {b["task_id"]: b for b in broken}
    assert "b" not in by_id
    assert "d" not in by_id
    assert by_id["c"]["missing"] == ["nope"]
    assert by_id["e"]["missing"] == ["other/missing"]
    db.close()


def test_find_broken_deps_dead_peer(tmp_path: Path) -> None:
    db = open_db(tmp_path / "q.db")
    _seed(db, "p", "up", status="failed")
    _seed(db, "p", "down", depends_on=["up"])
    broken = find_broken_deps(db)
    assert len(broken) == 1
    assert broken[0]["task_id"] == "down"
    assert broken[0]["missing"] == []
    assert broken[0]["dead"] == [{"ref": "up", "status": "failed"}]
    db.close()


def test_deps_api_missing_edge_health_dead(tmp_path: Path) -> None:
    db = open_db(tmp_path / "q.db")
    _seed(db, "p", "leaf", depends_on=["ghost"])
    result = _get_deps_response(db, project="p")
    edges = result["edges"]
    assert len(edges) == 1
    assert edges[0]["from"] == "ghost"
    assert edges[0]["health"] == "dead"
    stubs = [n for n in result["nodes"] if n.get("stub") or n.get("status") == "missing"]
    assert any(n["id"] == "ghost" for n in stubs)
    db.close()


def test_queue_api_exposes_missing_deps(tmp_path: Path) -> None:
    db = open_db(tmp_path / "q.db")
    _seed(db, "p", "ok", status="completed")
    _seed(db, "p", "broken", depends_on=["ghost", "ok"])
    result = _get_queue_response(db, project="p", limit=50)
    assert any(b["task_id"] == "broken" for b in result["broken_deps"])
    tasks = {t["task_id"]: t for t in result["tasks"]}
    assert tasks["broken"]["missing_deps"] == ["ghost"]
    assert tasks["ok"]["missing_deps"] == []
    db.close()


def test_list_multi_status(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    db = open_db(home / "queue.db")
    _seed(db, "p", "f1", status="failed")
    _seed(db, "p", "q1", status="queued")
    _seed(db, "p", "d1", status="disabled", enabled=0)
    _seed(db, "p", "c1", status="completed")
    db.close()

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cmd_list(home, None, status_filter="failed,disabled", as_json=True)
    assert rc == 0
    rows = json.loads(buf.getvalue())
    ids = {r["task_id"] for r in rows}
    assert ids == {"f1", "d1"}


def test_cmd_health_and_attention(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    db = open_db(home / "queue.db")
    _seed(db, "p", "ok", status="completed")
    _seed(db, "p", "broken", depends_on=["ghost"])
    _seed(db, "p", "bad", status="failed")
    _seed(db, "p", "off", status="disabled", enabled=0)
    db.execute(
        "UPDATE tasks SET last_error_code='tq.run.nonzero' WHERE task_id='bad'"
    )
    db.commit()
    db.close()

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cmd_health(home, None, as_json=True)
    assert rc == 1
    report = json.loads(buf.getvalue())
    assert report["ok"] is False
    assert any(b["task_id"] == "broken" for b in report["missing_deps"])
    assert any(a["task_id"] == "bad" for a in report["attention"])
    assert any(d["task_id"] == "off" for d in report["disabled"])

    buf2 = io.StringIO()
    with redirect_stdout(buf2):
        rc2 = cmd_attention(home, "p", as_json=True)
    assert rc2 == 1
    attn = json.loads(buf2.getvalue())
    assert attn["count"] >= 1


def test_build_task_health_ok_when_clean(tmp_path: Path) -> None:
    db = open_db(tmp_path / "q.db")
    _seed(db, "p", "a", status="completed")
    _seed(db, "p", "b", status="queued", depends_on=["a"])
    report = build_task_health(db, project="p")
    # b is waiting on completed a — no missing; attention empty unless error codes
    assert report["missing_deps"] == []
    assert report["ok"] is True
    db.close()


def test_mcp_task_health_tool(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    db = open_db(home / "queue.db")
    _seed(db, "p", "t", depends_on=["ghost"])
    db.close()
    names = {t["name"] for t in TOOLS}
    assert "task_health" in names
    result = _call_tool(home, "task_health", {"project": "p"})
    assert result["ok"] is False
    assert result["missing_deps"][0]["task_id"] == "t"
