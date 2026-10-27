"""Pipeline doctrine: suite/group retry completed, retry --downstream, deps API."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest


def _open(tmp_path: Path) -> sqlite3.Connection:
    from taskqueue.db import open_db

    db = tmp_path / "queue.db"
    return open_db(db)


def _insert(
    conn: sqlite3.Connection,
    project: str,
    task_id: str,
    *,
    status: str,
    depends_on=None,
    suite="",
    grp="",
    block_reason: str | None = None,
    last_error_code: str | None = None,
):
    conn.execute(
        "INSERT INTO tasks (project, task_id, command, status, enqueued_at, updated_at, "
        "depends_on, suite, grp, enabled) VALUES (?,?,?,?,datetime('now'),datetime('now'),?,?,?,1)",
        (
            project,
            task_id,
            "python",
            status,
            json.dumps(depends_on or []),
            suite,
            grp,
        ),
    )
    if block_reason is not None or last_error_code is not None:
        conn.execute(
            "UPDATE tasks SET block_reason = ?, last_error_code = ? "
            "WHERE project = ? AND task_id = ?",
            (block_reason, last_error_code, project, task_id),
        )
    conn.commit()


def test_suite_retry_includes_completed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from taskqueue._cli_project import cmd_suite

    home = tmp_path / "home"
    home.mkdir()
    conn = _open(home)
    _insert(conn, "p", "a", status="completed", suite="s1")
    _insert(conn, "p", "b", status="failed", suite="s1")
    conn.close()

    monkeypatch.setenv("TASKQUEUE_HOME", str(home))
    # cmd_suite uses _db(home) via home arg
    rc = cmd_suite(home, "retry", "s1")
    assert rc == 0
    conn = _open(home)
    statuses = {
        r["task_id"]: r["status"]
        for r in conn.execute("SELECT task_id, status FROM tasks WHERE suite='s1'").fetchall()
    }
    conn.close()
    assert statuses["a"] == "queued"
    assert statuses["b"] == "queued"


def test_collect_and_retry_downstream(tmp_path: Path):
    from taskqueue.db import collect_downstream_task_ids, retry_task_with_downstream

    home = tmp_path / "home"
    home.mkdir()
    conn = _open(home)
    _insert(conn, "p", "a", status="completed")
    _insert(conn, "p", "check", status="completed", depends_on=["a"])
    _insert(conn, "p", "b", status="completed", depends_on=["check"])
    _insert(conn, "p", "other", status="completed", depends_on=["x"])

    assert collect_downstream_task_ids(conn, "p", "a") == ["check", "b"]
    result = retry_task_with_downstream(conn, "p", "a")
    assert set(result["requeued"]) == {"a", "check", "b"}
    assert result["skipped"] == []
    statuses = {
        r["task_id"]: r["status"]
        for r in conn.execute("SELECT task_id, status FROM tasks WHERE project='p'").fetchall()
    }
    assert statuses["a"] == "queued"
    assert statuses["check"] == "queued"
    assert statuses["b"] == "queued"
    assert statuses["other"] == "completed"
    conn.close()


def test_deps_response_edge_health_and_focus(tmp_path: Path):
    from taskqueue.web import _get_deps_response

    home = tmp_path / "home"
    home.mkdir()
    conn = _open(home)
    _insert(conn, "p", "a", status="failed")
    _insert(conn, "p", "b", status="queued", depends_on=["a"])
    _insert(conn, "p", "c", status="completed", depends_on=["b"])

    full = _get_deps_response(conn, project="p")
    assert full["mode"] == "all"
    by_to = {e["to"]: e for e in full["edges"]}
    assert by_to["b"]["health"] == "dead"
    assert by_to["c"]["health"] == "wait"

    focused = _get_deps_response(conn, project="p", focus="b")
    ids = {n["id"] for n in focused["nodes"]}
    assert ids == {"a", "b", "c"}
    conn.close()


def test_deps_response_suite_filter(tmp_path: Path):
    from taskqueue.web import _get_deps_response

    home = tmp_path / "home"
    home.mkdir()
    conn = _open(home)
    _insert(conn, "p", "a", status="completed", suite="loc", depends_on=[])
    _insert(conn, "p", "b", status="queued", suite="loc", depends_on=["a"])
    _insert(conn, "p", "c", status="queued", suite="other", depends_on=["a"])
    r = _get_deps_response(conn, project="p", suite="loc")
    assert r["suite"] == "loc"
    ids = {n["id"] for n in r["nodes"]}
    assert ids == {"a", "b"}
    assert all(e["from"] in ids and e["to"] in ids for e in r["edges"])
    conn.close()


def _seed_deps_mode_fixture(conn: sqlite3.Connection) -> None:
    # isolated completed — should drop in active/attention
    _insert(conn, "p", "A", status="completed")
    # completed B → queued C
    _insert(conn, "p", "B", status="completed")
    _insert(conn, "p", "C", status="queued", depends_on=["B"])
    # running isolated
    _insert(conn, "p", "D", status="running")
    # failed F with completed upstream U
    _insert(conn, "p", "U", status="completed")
    _insert(conn, "p", "F", status="failed", depends_on=["U"])
    # blocked queued
    _insert(conn, "p", "Q", status="queued", block_reason="waiting on deps")
    # suspended
    _insert(conn, "p", "S", status="suspended")


def test_deps_mode_active(tmp_path: Path):
    from taskqueue.web import _get_deps_response

    home = tmp_path / "home"
    home.mkdir()
    conn = _open(home)
    _seed_deps_mode_fixture(conn)
    r = _get_deps_response(conn, project="p", mode="active")
    by_id = {n["id"]: n for n in r["nodes"]}
    assert "A" not in by_id
    assert set(by_id) == {"B", "C", "D", "F", "U", "Q", "S"}
    assert by_id["B"].get("stub") is True
    assert by_id["U"].get("stub") is True
    assert not by_id["C"].get("stub")
    assert not by_id["D"].get("stub")
    edge_pairs = {(e["from"], e["to"]) for e in r["edges"]}
    assert ("B", "C") in edge_pairs
    assert ("U", "F") in edge_pairs
    conn.close()


def test_deps_mode_attention(tmp_path: Path):
    from taskqueue.web import _get_deps_response

    home = tmp_path / "home"
    home.mkdir()
    conn = _open(home)
    _seed_deps_mode_fixture(conn)
    r = _get_deps_response(conn, project="p", mode="attention")
    by_id = {n["id"]: n for n in r["nodes"]}
    assert set(by_id) == {"F", "U", "Q"}
    assert by_id["U"].get("stub") is True
    assert "D" not in by_id
    assert "S" not in by_id
    assert "C" not in by_id
    assert "B" not in by_id
    conn.close()


def test_deps_mode_default_all(tmp_path: Path):
    from taskqueue.web import _get_deps_response

    home = tmp_path / "home"
    home.mkdir()
    conn = _open(home)
    _seed_deps_mode_fixture(conn)
    r = _get_deps_response(conn, project="p")
    ids = {n["id"] for n in r["nodes"]}
    assert ids == {"A", "B", "C", "D", "U", "F", "Q", "S"}
    assert r["mode"] == "all"
    conn.close()


def test_deps_mode_invalid(tmp_path: Path):
    from taskqueue.web import _apply_deps_mode, _get_deps_response
    from tests.helpers.master_worker import http_request, make_handler_env

    with pytest.raises(ValueError, match="invalid deps mode"):
        _apply_deps_mode([], [], "bogus")

    home = tmp_path / "home"
    home.mkdir()
    conn = _open(home)
    _insert(conn, "p", "t", status="queued")
    with pytest.raises(ValueError, match="invalid deps mode"):
        _get_deps_response(conn, project="p", mode="bogus")
    conn.close()

    HandlerClass, _db, _ = make_handler_env(tmp_path / "handler")
    code, body = http_request(HandlerClass, "GET", "/api/deps?mode=bogus")
    assert code == 400
    assert "error" in body


def test_suites_response_lists_named_suites(tmp_path: Path):
    from taskqueue.web import _get_suites_response

    home = tmp_path / "home"
    home.mkdir()
    conn = _open(home)
    _insert(conn, "p", "a", status="completed", suite="loc")
    _insert(conn, "p", "b", status="failed", suite="loc")
    _insert(conn, "p", "c", status="queued", suite="")
    r = _get_suites_response(conn, project="p")
    assert len(r["suites"]) == 1
    assert r["suites"][0]["name"] == "loc"
    assert r["suites"][0]["total"] == 2
    assert r["suites"][0]["projects"] == ["p"]
    conn.close()


def test_suites_response_lists_projects_across_all(tmp_path: Path):
    from taskqueue.web import _get_suites_response

    home = tmp_path / "home"
    home.mkdir()
    conn = _open(home)
    _insert(conn, "alpha", "a", status="completed", suite="shared")
    _insert(conn, "beta", "b", status="failed", suite="shared")
    _insert(conn, "beta", "c", status="queued", suite="other")
    r = _get_suites_response(conn)
    by_name = {s["name"]: s for s in r["suites"]}
    assert by_name["shared"]["projects"] == ["alpha", "beta"]
    assert by_name["shared"]["total"] == 2
    assert by_name["other"]["projects"] == ["beta"]
    conn.close()


def test_finalize_wake_hooks_on_complete(tmp_path: Path):
    from taskqueue.finalize_hooks import apply_finalize_wake_hooks

    home = tmp_path / "home"
    home.mkdir()
    conn = _open(home)
    _insert(conn, "p", "a", status="completed")
    conn.execute(
        "UPDATE tasks SET on_complete = ? WHERE project='p' AND task_id='a'",
        (json.dumps(["wake_me"]),),
    )
    _insert(conn, "p", "wake_me", status="completed")
    conn.commit()
    apply_finalize_wake_hooks(conn, "p", "a", "completed", run_teardown=False)
    st = conn.execute(
        "SELECT status FROM tasks WHERE project='p' AND task_id='wake_me'"
    ).fetchone()["status"]
    assert st == "queued"
    conn.close()


def test_web_suite_retry_includes_completed_group_retry_needs_explicit_restart_all(tmp_path: Path):
    """Suites re-run completed members by design; groups only via confirmed restart-all (2026-10-02)."""
    from tests.helpers.master_worker import http_request, make_handler_env

    HandlerClass, db, _ = make_handler_env(tmp_path)
    _insert(db, "p", "s_done", status="completed", suite="loc")
    _insert(db, "p", "s_fail", status="failed", suite="loc")
    _insert(db, "p", "g_done", status="completed", grp="pipe")
    _insert(db, "p", "g_fail", status="failed", grp="pipe")

    code, body = http_request(HandlerClass, "POST", "/api/suites/loc/retry")
    assert code == 200
    assert body.get("ok") is True
    assert body.get("retried") == 2

    # Plain group retry = "Retry failed": completed members stay completed.
    code, body = http_request(HandlerClass, "POST", "/api/groups/pipe/retry")
    assert code == 200
    assert body.get("ok") is True
    assert body.get("retried") == 1

    # Restart-all (incl. completed) needs ?completed=1 and the exact confirm count.
    code, body = http_request(HandlerClass, "POST", "/api/groups/pipe/retry?completed=1")
    assert code == 409 and body.get("would_retry") == 1
    code, body = http_request(HandlerClass, "POST", "/api/groups/pipe/retry?completed=1&confirm=1")
    assert code == 200 and body.get("retried") == 1

    statuses = {
        r["task_id"]: r["status"]
        for r in db.execute("SELECT task_id, status FROM tasks WHERE project='p'").fetchall()
    }
    assert statuses["s_done"] == "queued"
    assert statuses["s_fail"] == "queued"
    assert statuses["g_done"] == "queued"
    assert statuses["g_fail"] == "queued"


def test_suite_disable_without_yaml_falls_back_to_db(tmp_path: Path):
    from taskqueue._cli_project import cmd_suite

    home = tmp_path / "home"
    home.mkdir()
    conn = _open(home)
    _insert(conn, "p", "a", status="queued", suite="s2")
    conn.close()
    assert cmd_suite(home, "disable", "s2") == 0
    conn = _open(home)
    en = conn.execute("SELECT enabled FROM tasks WHERE task_id='a'").fetchone()[0]
    conn.close()
    assert en == 0


def test_suite_disable_patches_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import taskqueue.db as dbmod
    import taskqueue.yaml_patch as yp
    from taskqueue import config as cfg
    from taskqueue._cli_project import cmd_suite

    home = tmp_path / "home"
    home.mkdir()
    y = tmp_path / "t.yaml"
    y.write_text("task_id: a\n", encoding="utf-8")
    conn = _open(home)
    _insert(conn, "p", "a", status="queued", suite="s3")
    conn.execute("UPDATE tasks SET yaml_source=? WHERE task_id='a'", (str(y),))
    conn.commit()
    conn.close()
    patched = []
    monkeypatch.setattr(cfg, "load_projects", lambda h: {"p": object()})
    monkeypatch.setattr(yp, "patch_task_yaml", lambda path, upd, task_id=None: patched.append((str(path), upd, task_id)))
    monkeypatch.setattr(dbmod, "parse_task_yaml", lambda *a, **k: None)
    monkeypatch.setattr(dbmod, "upsert_task", lambda *a, **k: None)
    assert cmd_suite(home, "disable", "s3") == 0
    assert patched == [(str(y), {"enabled": False}, "a")]
