"""find_broken_deps skips dep-less rows; the cached variant recomputes only when tasks change."""

from __future__ import annotations

from pathlib import Path

from taskqueue import queries
from taskqueue.db import open_db


def _ins(conn, tid: str, status: str = "queued", deps: str = "[]", upd: str = "2026-10-23T00:00:00") -> None:
    conn.execute(
        "INSERT INTO tasks (project, task_id, command, status, enqueued_at, updated_at, depends_on) "
        "VALUES ('p', ?, 'x', ?, ?, ?, ?)",
        (tid, status, upd, upd, deps),
    )
    conn.commit()


def test_cached_recomputes_only_on_change(tmp_path: Path, monkeypatch) -> None:
    queries._BROKEN_CACHE.clear()
    conn = open_db(tmp_path / "tq.db")
    _ins(conn, "a")
    _ins(conn, "b", deps='["ghost"]')
    calls: list[int] = []
    real = queries.find_broken_deps
    monkeypatch.setattr(queries, "find_broken_deps", lambda *a, **k: (calls.append(1), real(*a, **k))[1])

    first = queries.find_broken_deps_cached(conn)
    assert [b["task_id"] for b in first] == ["b"] and first[0]["missing"]
    assert queries.find_broken_deps_cached(conn) == first
    assert len(calls) == 1

    _ins(conn, "c", deps='["ghost2"]', upd="2026-10-23T00:00:05")
    second = queries.find_broken_deps_cached(conn)
    assert {b["task_id"] for b in second} == {"b", "c"}
    assert len(calls) == 2
    conn.close()


def test_depless_rows_are_not_scanned(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "tq.db")
    for i in range(20):
        _ins(conn, f"n{i}")
    _ins(conn, "dead", deps='["n0"]')
    conn.execute("UPDATE tasks SET status='failed' WHERE task_id='n0'")
    conn.commit()
    out = queries.find_broken_deps(conn)
    assert [(b["task_id"], b["dead"][0]["status"]) for b in out] == [("dead", "failed")]
    conn.close()


def test_tasks_cached_invalidates_on_change(tmp_path: Path) -> None:
    queries._BROKEN_CACHE.clear()
    conn = open_db(tmp_path / "tq.db")
    _ins(conn, "a")
    n: list[int] = []
    compute = lambda: (n.append(1), len(n))[1]  # noqa: E731
    assert queries.tasks_cached(conn, "x", None, compute) == 1
    assert queries.tasks_cached(conn, "x", None, compute) == 1
    _ins(conn, "b", upd="2026-10-23T00:01:00")
    assert queries.tasks_cached(conn, "x", None, compute) == 2
    conn.close()


def test_deps_graph_is_capped_unless_focused(tmp_path: Path) -> None:
    from taskqueue.web.responses import DEPS_MAX_NODES, _get_deps_response

    conn = open_db(tmp_path / "tq.db")
    n = DEPS_MAX_NODES + 50
    conn.executemany(
        "INSERT INTO tasks (project, task_id, command, status, enqueued_at, updated_at, depends_on) "
        "VALUES ('p', ?, 'x', 'queued', 't', 't', ?)",
        [(f"t{i:04d}", '["t0000"]' if i else "[]") for i in range(n)],
    )
    conn.commit()
    r = _get_deps_response(conn)
    assert r["truncated"] is True and r["total_nodes"] == n and len(r["nodes"]) == DEPS_MAX_NODES
    ids = {x["id"] for x in r["nodes"]}
    assert all(e["from"] in ids and e["to"] in ids for e in r["edges"])
    f = _get_deps_response(conn, project="p", focus="t0000")
    assert f["truncated"] is False and len(f["nodes"]) == n
    conn.close()
