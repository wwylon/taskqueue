"""Shared read queries for attention and operator task lists."""

from __future__ import annotations
from taskqueue._time import parse_iso as _parse_iso

import sqlite3
from typing import Any

# Peers that cannot satisfy depends_on (UI paints these as dead; admission blocks).
DEAD_DEP_STATUSES = frozenset({"failed", "stopped", "interrupted", "disabled", "cancelled"})


from taskqueue.deprefs import resolve_dep_ref, row_dep_refs  # noqa: E402,F401


_CHUNK = 400  # max (project, task_id) pairs per VALUES CTE — keeps SQLite under 999 bind vars


def _fetch_status_map(
    conn: sqlite3.Connection,
    pairs: set[tuple[str, str]],
) -> dict[tuple[str, str], str]:
    """Fetch status for a set of (project, task_id) pairs, chunked to stay under SQLite's
    999-variable limit (2 vars per pair → max 499 pairs per query; we use 400 for headroom)."""
    result: dict[tuple[str, str], str] = {}
    pairs_list = list(pairs)
    for i in range(0, len(pairs_list), _CHUNK):
        chunk = pairs_list[i : i + _CHUNK]
        placeholders = ", ".join("(?, ?)" for _ in chunk)
        flat = [v for pair in chunk for v in pair]
        for r in conn.execute(
            f"WITH _need(p, t) AS (VALUES {placeholders}) "
            "SELECT tasks.project, tasks.task_id, tasks.status "
            "FROM tasks JOIN _need ON tasks.project = _need.p AND tasks.task_id = _need.t",
            flat,
        ):
            result[(str(r["project"]), str(r["task_id"]))] = str(r["status"])
    return result


def find_broken_deps(
    conn: sqlite3.Connection,
    *,
    project: str | None = None,
    collect_disabled: bool = False,
) -> "list[dict[str, Any]] | tuple[list[dict[str, Any]], list[dict[str, Any]]]":
    """Tasks whose AND/OR dependency peers are missing or in a dead terminal status.

    ``depends_on`` (AND): broken if *any* peer is missing/dead.
    ``depends_on_any`` (OR): broken only if *every* peer is missing/dead.

    Each item: ``{project, task_id, missing: [label…], dead: [{ref, status}…]}``.
    Status lookup is always global so cross-project refs resolve correctly.

    When *collect_disabled* is ``True``, returns ``(broken, disabled)`` where
    *disabled* contains rows with ``status='disabled'``, collected in the same
    table scan at no extra SQL cost.
    """
    extra = ", status, enabled, updated_at" if collect_disabled else ""
    sql = f"SELECT project, task_id, depends_on, depends_on_any{extra} FROM tasks"
    params: list[Any] = []
    where: list[str] = []
    if project:
        where.append("project = ?")
        params.append(project)
    if not collect_disabled:
        # Rows without dependencies can never be broken; skip parsing them (most of a big queue).
        where.append(
            "(COALESCE(depends_on, '[]') NOT IN ('', '[]') "
            "OR COALESCE(depends_on_any, '[]') NOT IN ('', '[]'))"
        )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY project, task_id"
    candidate_rows = conn.execute(sql, params).fetchall()

    # Status lookup covers only tasks referenced by these rows (no full-table scan).
    needed: set[tuple[str, str]] = {(str(r["project"]), str(r["task_id"])) for r in candidate_rows}
    for row in candidate_rows:
        owner = str(row["project"])
        for col in ("depends_on", "depends_on_any"):
            needed.update((dp, di) for dp, di, _ in row_dep_refs(row, col, owner))
    status_map = _fetch_status_map(conn, needed) if needed else {}
    out: list[dict[str, Any]] = []
    for row in candidate_rows:
        missing: list[str] = []
        dead: list[dict[str, str]] = []
        owner = str(row["project"])

        for dep_proj, dep_id, label in row_dep_refs(row, "depends_on", owner):
            st = status_map.get((dep_proj, dep_id))
            if st is None:
                missing.append(label)
            elif st in DEAD_DEP_STATUSES:
                dead.append({"ref": label, "status": st})

        deps_any = row_dep_refs(row, "depends_on_any", owner)
        if deps_any:
            any_ok = False
            or_missing: list[str] = []
            or_dead: list[dict[str, str]] = []
            for dep_proj, dep_id, label in deps_any:
                st = status_map.get((dep_proj, dep_id))
                if st is None:
                    or_missing.append(label)
                elif st in DEAD_DEP_STATUSES:
                    or_dead.append({"ref": label, "status": st})
                else:
                    any_ok = True  # queued/running/completed — OR can still succeed
                    break
            if not any_ok:
                missing.extend(or_missing)
                dead.extend(or_dead)

        if missing or dead:
            out.append({
                "project": owner,
                "task_id": str(row["task_id"]),
                "missing": missing,
                "dead": dead,
            })

    if collect_disabled:
        disabled_out: list[dict[str, Any]] = [
            {
                "project": str(r["project"]),
                "task_id": str(r["task_id"]),
                "status": str(r["status"]),
                "enabled": r["enabled"],
                "updated_at": r["updated_at"],
            }
            for r in candidate_rows
            if str(r["status"]) == "disabled"
        ]
        return out, disabled_out
    return out


_BROKEN_CACHE: dict[tuple[str, str], tuple[tuple, float, list[dict[str, Any]]]] = {}
_BROKEN_CACHE_TTL_SEC = 30.0


def tasks_cached(conn: sqlite3.Connection, name: str, project: str | None, compute, ttl: float = 10.0):
    """Memoise ``compute()`` on a tasks fingerprint (row count + newest updated_at) with a TTL."""
    import time

    row = conn.execute(
        "SELECT COUNT(*), MAX(updated_at), "
        "(SELECT file FROM pragma_database_list WHERE name = 'main') FROM tasks"
        + (" WHERE project = ?" if project else ""),
        (project,) if project else (),
    ).fetchone()
    fp = (row[0], row[1])
    key = (f"{name}|{row[2]}", project or "")
    now = time.monotonic()
    hit = _BROKEN_CACHE.get(key)
    if hit is not None and hit[0] == fp and now - hit[1] < ttl:
        return hit[2]
    result = compute()
    _BROKEN_CACHE[key] = (fp, now, result)
    return result


def find_broken_deps_cached(
    conn: sqlite3.Connection, *, project: str | None = None
) -> list[dict[str, Any]]:
    """``find_broken_deps`` memoised on a cheap tasks fingerprint (row count + newest updated_at).

    The dashboard asks for this on every refresh; it is O(all tasks), while the fingerprint is an
    index-only query. Every task write bumps ``updated_at``; the TTL bounds any missed edge.
    """
    import time

    row = conn.execute(
        "SELECT COUNT(*), MAX(updated_at), "
        "(SELECT file FROM pragma_database_list WHERE name = 'main') FROM tasks"
        + (" WHERE project = ?" if project else ""),
        (project,) if project else (),
    ).fetchone()
    fp = (row[0], row[1])
    key = (str(row[2]), project or "")
    now = time.monotonic()
    hit = _BROKEN_CACHE.get(key)
    if hit is not None and hit[0] == fp and now - hit[1] < _BROKEN_CACHE_TTL_SEC:
        return hit[2]
    result = find_broken_deps(conn, project=project)
    _BROKEN_CACHE[key] = (fp, now, result)
    return result


# Dependency states that trigger ``on_dependency_failure: cancel``. Deliberately
# narrower than DEAD_DEP_STATUSES: ``stopped``/``interrupted`` are auto-restartable
# and ``disabled`` is an operator action, so none of them should destroy a chain.
CANCEL_TRIGGER_STATUSES = frozenset({"failed", "cancelled"})


def cancel_dead_dependents(
    conn: sqlite3.Connection,
    home: Any = None,
    cfg: dict[str, Any] | None = None,
) -> list[tuple[str, str]]:
    """T-206: move queued ``on_dependency_failure: cancel`` tasks to ``cancelled``.

    A task is cancelled when any ``depends_on`` peer (or every ``depends_on_any``
    peer) is in ``CANCEL_TRIGGER_STATUSES``. Cascades one hop per call, so a
    chain settles over successive ticks. Returns the ``(project, task_id)`` pairs
    cancelled.
    """
    from taskqueue import errors as tqerr
    from taskqueue._db_tasks import set_task_error

    rows = conn.execute(
        "SELECT project, task_id, depends_on, depends_on_any, description, suite "
        "FROM tasks WHERE status = 'queued' AND enabled = 1 "
        "AND on_dependency_failure = 'cancel'"
    ).fetchall()
    if not rows:
        return []
    needed: set[tuple[str, str]] = set()
    for r in rows:
        for col in ("depends_on", "depends_on_any"):
            needed.update((dp, di) for dp, di, _ in row_dep_refs(r, col, str(r["project"])))
    status_map = _fetch_status_map(conn, needed) if needed else {}
    cancelled: list[tuple[str, str]] = []
    for r in rows:
        owner = str(r["project"])
        culprit = ""
        for dp, di, label in row_dep_refs(r, "depends_on", owner):
            st = status_map.get((dp, di))
            if st in CANCEL_TRIGGER_STATUSES:
                culprit = f"{label} [{st}]"
                break
        if not culprit:
            any_refs = row_dep_refs(r, "depends_on_any", owner)
            if any_refs:
                dead = []
                for dp, di, label in any_refs:
                    st = status_map.get((dp, di))
                    if st not in CANCEL_TRIGGER_STATUSES:
                        dead = []
                        break
                    dead.append(f"{label} [{st}]")
                if dead:
                    culprit = ", ".join(dead)
        if not culprit:
            continue
        now = _utc_now_iso()
        cur = conn.execute(
            "UPDATE tasks SET status = 'cancelled', finished_at = ?, updated_at = ? "
            "WHERE project = ? AND task_id = ? AND status = 'queued'",
            (now, now, owner, r["task_id"]),
        )
        if not cur.rowcount:
            continue
        set_task_error(
            conn, owner, r["task_id"], tqerr.DEP_CANCELLED,
            block_reason=f"cancelled: dependency failed — {culprit}", commit=False,
        )
        cancelled.append((owner, str(r["task_id"])))
    conn.commit()
    if home is not None:
        from taskqueue.notify import emit_task_notify

        _cfg = dict(cfg or {})
        _cfg["toast_notifications"] = False  # no desktop toast per cascaded cancel
        for proj, tid in cancelled:
            emit_task_notify(
                home, project=proj, task_id=tid, status="cancelled", run_id=None,
                exit_code=None, last_error_code=tqerr.DEP_CANCELLED,
                notify_url=None, started_by=None, cfg=_cfg,
                block_reason="cancelled: dependency failed",
            )
    return cancelled


def cancel_negative_dependents(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Cancel queued ``on_dependency_negative: cancel`` tasks whose dependency peer(s) completed with a
    negative result (``tq.run.negative``). Default ``run`` lets dependents run, as for any completion.

    ``depends_on``: cancel when **any** peer ended negative.
    ``depends_on_any`` (F-115): cancel when **every** peer ended negative (mirrors dead-dep OR semantics).
    """
    from taskqueue import errors as tqerr
    from taskqueue._db_tasks import set_task_error

    rows = conn.execute(
        "SELECT project, task_id, depends_on, depends_on_any FROM tasks "
        "WHERE status = 'queued' AND enabled = 1 AND on_dependency_negative = 'cancel'"
    ).fetchall()
    cancelled: list[tuple[str, str]] = []
    for r in rows:
        owner = str(r["project"])
        culprit = ""
        for dp, di, label in row_dep_refs(r, "depends_on", owner):
            dep = conn.execute(
                "SELECT status, last_error_code FROM tasks WHERE project = ? AND task_id = ?", (dp, di)
            ).fetchone()
            if dep is not None and dep["status"] == "completed" and dep["last_error_code"] == tqerr.RUN_NEGATIVE:
                culprit = label
                break
        if not culprit:
            any_refs = row_dep_refs(r, "depends_on_any", owner)
            if any_refs:
                dead: list[str] = []
                for dp, di, label in any_refs:
                    dep = conn.execute(
                        "SELECT status, last_error_code FROM tasks WHERE project = ? AND task_id = ?",
                        (dp, di),
                    ).fetchone()
                    if (
                        dep is None
                        or dep["status"] != "completed"
                        or dep["last_error_code"] != tqerr.RUN_NEGATIVE
                    ):
                        dead = []
                        break
                    dead.append(label)
                if dead:
                    culprit = ", ".join(dead)
        if not culprit:
            continue
        now = _utc_now_iso()
        cur = conn.execute(
            "UPDATE tasks SET status = 'cancelled', finished_at = ?, updated_at = ? "
            "WHERE project = ? AND task_id = ? AND status = 'queued'",
            (now, now, owner, r["task_id"]),
        )
        if cur.rowcount:
            set_task_error(conn, owner, r["task_id"], tqerr.DEP_NEGATIVE,
                           block_reason=f"cancelled: dependency {culprit} ended negative", commit=False)
            cancelled.append((owner, str(r["task_id"])))
    conn.commit()
    return cancelled


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def status_counts(
    conn: sqlite3.Connection,
    *,
    project: str | None = None,
) -> dict[str, int]:
    """Task counts keyed by status."""
    sql = "SELECT status, COUNT(*) AS n FROM tasks"
    params: list[Any] = []
    if project:
        sql += " WHERE project = ?"
        params.append(project)
    sql += " GROUP BY status"
    return {str(r["status"]): int(r["n"]) for r in conn.execute(sql, params)}


def list_disabled(
    conn: sqlite3.Connection,
    *,
    project: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Disabled tasks (status=disabled)."""
    sql = (
        "SELECT project, task_id, status, enabled, updated_at "
        "FROM tasks WHERE status = 'disabled'"
    )
    params: list[Any] = []
    if project:
        sql += " AND project = ?"
        params.append(project)
    sql += " ORDER BY updated_at DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def build_task_health(
    conn: sqlite3.Connection,
    *,
    project: str | None = None,
    attention_limit: int = 500,
) -> dict[str, Any]:
    """Full submitted-task health report for CLI / MCP."""
    broken, disabled = find_broken_deps(conn, project=project, collect_disabled=True)
    missing_only = [b for b in broken if b.get("missing")]
    attention = list_attention(conn, project=project, limit=attention_limit)
    return {
        "counts": status_counts(conn, project=project),
        "broken_deps": broken,
        "missing_deps": missing_only,
        "attention": attention,
        "disabled": disabled,
        "ok": not missing_only and not attention,
    }


def list_attention(
    conn: sqlite3.Connection,
    *,
    operator: str | None = None,
    project: str | None = None,
    since: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Tasks needing operator attention (errors / failed / interrupted / suspended)."""
    from taskqueue.errors import describe

    sql = (
        "SELECT t.project, t.task_id, t.status, t.last_error_code, t.block_reason, "
        "t.started_by, t.updated_at, "
        "r.started_at, r.finished_at "
        "FROM tasks t "
        "LEFT JOIN runs r ON r.run_id = ("
        "  SELECT run_id FROM runs WHERE project = t.project AND task_id = t.task_id "
        "  ORDER BY run_id DESC LIMIT 1"
        ") "
        "WHERE (t.last_error_code IS NOT NULL "
        "OR t.status IN ('failed', 'interrupted', 'suspended', 'stopped', 'unknown', 'cancelled'))"
    )
    params: list[Any] = []
    if operator:
        sql += " AND t.started_by = ?"
        params.append(operator)
    if project:
        sql += " AND t.project = ?"
        params.append(project)
    if since:
        sql += " AND t.updated_at >= ?"
        params.append(since)
    sql += " ORDER BY t.updated_at DESC LIMIT ?"
    params.append(limit)
    from datetime import datetime, timezone
    _now = datetime.now(timezone.utc)
    items: list[dict[str, Any]] = []
    for row in conn.execute(sql, params).fetchall():
        d = dict(row)
        d["error_description"] = describe(d.get("last_error_code"))
        elapsed_sec = None
        try:
            started_at = d.pop("started_at", None)
            finished_at = d.pop("finished_at", None)
            if started_at:
                started = _parse_iso(started_at)
                if finished_at:
                    finished = _parse_iso(finished_at)
                    elapsed_sec = round((finished - started).total_seconds(), 1)
                else:
                    elapsed_sec = round((_now - started).total_seconds(), 1)
        except Exception:
            pass
        d["elapsed_sec"] = elapsed_sec
        items.append(d)
    return items


def count_tasks(
    conn: sqlite3.Connection,
    *,
    started_by: str | None = None,
    project: str | None = None,
    search: str = "",
) -> int:
    """Count tasks matching the same filters as :func:`list_tasks` (F-141)."""
    sql = "SELECT COUNT(*) FROM tasks WHERE 1=1"
    params: list[Any] = []
    if started_by:
        sql += " AND started_by = ?"
        params.append(started_by)
    if project:
        sql += " AND project = ?"
        params.append(project)
    q = (search or "").strip()
    if q:
        pat = f"%{q}%"
        sql += " AND (task_id LIKE ? OR IFNULL(description, '') LIKE ?)"
        params.extend([pat, pat])
    row = conn.execute(sql, params).fetchone()
    return int(row[0]) if row else 0


def list_tasks(
    conn: sqlite3.Connection,
    *,
    started_by: str | None = None,
    project: str | None = None,
    search: str = "",
    limit: int = 500,
    offset: int = 0,
) -> list[sqlite3.Row]:
    """Filtered task rows (optional started_by / project / search)."""
    sql = "SELECT * FROM tasks WHERE 1=1"
    params: list[Any] = []
    if started_by:
        sql += " AND started_by = ?"
        params.append(started_by)
    if project:
        sql += " AND project = ?"
        params.append(project)
    q = (search or "").strip()
    if q:
        pat = f"%{q}%"
        sql += " AND (task_id LIKE ? OR IFNULL(description, '') LIKE ?)"
        params.extend([pat, pat])
    sql += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, max(0, offset)])
    return list(conn.execute(sql, params).fetchall())


def list_operator_tasks(
    conn: sqlite3.Connection,
    *,
    operator: str,
    project: str | None = None,
    limit: int = 500,
) -> list[sqlite3.Row]:
    """Tasks owned by an operator (started_by)."""
    return list_tasks(conn, started_by=operator, project=project, limit=limit)


def list_operator_projects(conn: sqlite3.Connection, *, operator: str) -> list[str]:
    """Distinct projects where operator started or submitted a task (R8 my-projects)."""
    op = (operator or "").strip()
    if not op:
        return []
    rows = conn.execute(
        "SELECT DISTINCT project FROM tasks "
        "WHERE started_by = ? OR submitted_by = ? "
        "ORDER BY project",
        (op, op),
    ).fetchall()
    return [str(r[0]) for r in rows if r[0]]


def list_operator_task_summaries(
    conn: sqlite3.Connection,
    *,
    operator: str,
    project: str | None = None,
) -> list[sqlite3.Row]:
    """Operator tasks with latest-run progress (CLI my-tasks)."""
    sql = (
        "SELECT t.project, t.task_id, t.status, t.last_error_code, r.progress_pct, r.worker_id "
        "FROM tasks t LEFT JOIN runs r ON r.run_id = ("
        "  SELECT run_id FROM runs WHERE project = t.project AND task_id = t.task_id "
        "  ORDER BY run_id DESC LIMIT 1"
        ") WHERE t.started_by = ?"
    )
    params: list[Any] = [operator]
    if project:
        sql += " AND t.project = ?"
        params.append(project)
    sql += " ORDER BY t.updated_at DESC"
    return list(conn.execute(sql, params).fetchall())
