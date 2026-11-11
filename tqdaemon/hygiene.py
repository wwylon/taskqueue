"""Queue hygiene: churn rates, multi-use vs flaky, project over-use → archive proposals.

Never auto-archives. Proposals must match what ``tq archive --max-completed-runs`` can do.
``tasks_added`` counts new task *rows* (``enqueued_at``), not YAML mtime touches.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from tqdaemon.archive import _movable

MULTI_USE_MIN_COMPLETED = 5
MULTI_USE_SPAN_DAYS = 7
MULTI_USE_WINDOW_DAYS = 3  # completed on >= this many distinct days in window
FLAKY_MIN_TOTAL = 3
ARCHIVE_PROPOSAL_PROJECTS = 3
ARCHIVE_SAMPLE_IDS = 20
TOP_PROJECTS = 15


def build_hygiene_report(
    conn,
    *,
    days: int = 7,
    project: str | None = None,
    older_than_days: float = 30.0,
    now: datetime | None = None,
) -> dict[str, Any]:
    days = max(1, min(int(days), 365))
    older_than_days = max(0.0, float(older_than_days))
    ref = now or datetime.now(UTC).replace(tzinfo=None)
    since = (ref - timedelta(days=days)).isoformat()
    since_24h = (ref - timedelta(hours=24)).isoformat()
    archive_cutoff = (ref - timedelta(days=older_than_days)).isoformat()
    ref_iso = ref.isoformat()

    proj_sql = ""
    params_base: list[Any] = []
    if project:
        proj_sql = " AND project = ?"
        params_base = [project]

    churn = _churn(conn, since=since, since_24h=since_24h, days=days, proj_sql=proj_sql, params_base=params_base)
    multi_use, flaky = _classify_tasks(
        conn,
        since=since,
        proj_sql=proj_sql,
        params_base=params_base,
        ref=ref,
    )
    multi_keys = {(m["project"], m["task_id"]) for m in multi_use}
    projects = _project_ranks(
        conn,
        since=since,
        archive_cutoff=archive_cutoff,
        proj_sql=proj_sql,
        params_base=params_base,
        multi_keys=multi_keys,
        older_than_days=older_than_days,
    )
    proposals = [p for p in projects if int(p.get("archivable_count") or 0) > 0][:ARCHIVE_PROPOSAL_PROJECTS]

    payload = {
        "kind": "hygiene",
        "days": days,
        "since": since,
        "as_of": ref_iso,
        "project": project,
        "older_than_days": older_than_days,
        "notes": {
            "tasks_added": "New task rows by enqueued_at (not YAML mtime / retries).",
            "multi_use": (
                f"completed_run_count>={MULTI_USE_MIN_COMPLETED} with "
                f">={MULTI_USE_SPAN_DAYS}d span, or >={MULTI_USE_WINDOW_DAYS} completed days in window."
            ),
            "archive": "Never auto-applied; use dry_run_command then drop --dry-run only if approved.",
        },
        "churn": churn,
        "multi_use": multi_use[:30],
        "flaky": flaky[:30],
        "projects": projects[:TOP_PROJECTS],
        "archive_proposals": proposals,
    }
    payload["agent_prompt"] = _agent_prompt(payload)
    return payload


def _churn(
    conn,
    *,
    since: str,
    since_24h: str,
    days: int,
    proj_sql: str,
    params_base: list[Any],
) -> dict[str, Any]:
    added = int(
        conn.execute(
            f"SELECT COUNT(*) FROM tasks WHERE enqueued_at >= ?{proj_sql}",
            [since, *params_base],
        ).fetchone()[0]
    )
    added_24h = int(
        conn.execute(
            f"SELECT COUNT(*) FROM tasks WHERE enqueued_at >= ?{proj_sql}",
            [since_24h, *params_base],
        ).fetchone()[0]
    )
    started = int(
        conn.execute(
            f"SELECT COUNT(*) FROM runs WHERE started_at >= ?{proj_sql}",
            [since, *params_base],
        ).fetchone()[0]
    )
    started_24h = int(
        conn.execute(
            f"SELECT COUNT(*) FROM runs WHERE started_at >= ?{proj_sql}",
            [since_24h, *params_base],
        ).fetchone()[0]
    )
    by_status = {
        str(r["status"] or "unknown"): int(r["n"])
        for r in conn.execute(
            f"SELECT status, COUNT(*) AS n FROM runs WHERE started_at >= ?{proj_sql} GROUP BY status",
            [since, *params_base],
        )
    }
    r_proj = " AND r.project = ?" if params_base else ""
    # Re-runs: starts in window that are not the task's first run_id.
    reruns = int(
        conn.execute(
            f"""
            SELECT COUNT(*) FROM runs r
            WHERE r.started_at >= ?{r_proj}
              AND r.run_id > (
                SELECT MIN(r2.run_id) FROM runs r2
                WHERE r2.project = r.project AND r2.task_id = r.task_id
              )
            """,
            [since, *params_base],
        ).fetchone()[0]
    )
    reruns_24h = int(
        conn.execute(
            f"""
            SELECT COUNT(*) FROM runs r
            WHERE r.started_at >= ?{r_proj}
              AND r.run_id > (
                SELECT MIN(r2.run_id) FROM runs r2
                WHERE r2.project = r.project AND r2.task_id = r.task_id
              )
            """,
            [since_24h, *params_base],
        ).fetchone()[0]
    )

    per_day_starts = [
        {"day": r["day"], "n": int(r["n"])}
        for r in conn.execute(
            f"""
            SELECT substr(started_at, 1, 10) AS day, COUNT(*) AS n
            FROM runs WHERE started_at >= ?{proj_sql}
            GROUP BY day ORDER BY day
            """,
            [since, *params_base],
        )
    ]
    per_day_added = [
        {"day": r["day"], "n": int(r["n"])}
        for r in conn.execute(
            f"""
            SELECT substr(enqueued_at, 1, 10) AS day, COUNT(*) AS n
            FROM tasks WHERE enqueued_at >= ?{proj_sql}
            GROUP BY day ORDER BY day
            """,
            [since, *params_base],
        )
    ]
    by_project_starts = [
        {"project": r["project"], "n": int(r["n"])}
        for r in conn.execute(
            f"""
            SELECT project, COUNT(*) AS n FROM runs
            WHERE started_at >= ?{proj_sql}
            GROUP BY project ORDER BY n DESC LIMIT ?
            """,
            [since, *params_base, TOP_PROJECTS],
        )
    ]
    by_project_added = [
        {"project": r["project"], "n": int(r["n"])}
        for r in conn.execute(
            f"""
            SELECT project, COUNT(*) AS n FROM tasks
            WHERE enqueued_at >= ?{proj_sql}
            GROUP BY project ORDER BY n DESC LIMIT ?
            """,
            [since, *params_base, TOP_PROJECTS],
        )
    ]
    return {
        "tasks_added": added,
        "tasks_added_last_24h": added_24h,
        "tasks_added_per_day_avg": round(added / days, 2),
        "runs_started": started,
        "runs_started_last_24h": started_24h,
        "runs_started_per_day_avg": round(started / days, 2),
        "runs_by_status": by_status,
        "reruns_started": reruns,
        "reruns_started_last_24h": reruns_24h,
        "reruns_per_day_avg": round(reruns / days, 2),
        "per_day_runs_started": per_day_starts,
        "per_day_tasks_added": per_day_added,
        "by_project_runs_started": by_project_starts,
        "by_project_tasks_added": by_project_added,
    }


def _classify_tasks(
    conn,
    *,
    since: str,
    proj_sql: str,
    params_base: list[Any],
    ref: datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = conn.execute(
        f"""
        SELECT project, task_id, status, updated_at,
               COALESCE(total_run_count, 0) AS total_run_count,
               COALESCE(completed_run_count, 0) AS completed_run_count
        FROM tasks WHERE 1=1{proj_sql}
        """,
        params_base,
    ).fetchall()
    # Completed-day counts in window for multi-use alternate rule.
    day_rows = conn.execute(
        f"""
        SELECT project, task_id, COUNT(DISTINCT substr(finished_at, 1, 10)) AS days
        FROM runs
        WHERE status = 'completed' AND finished_at >= ?{proj_sql}
        GROUP BY project, task_id
        """,
        [since, *params_base],
    ).fetchall()
    days_map = {(r["project"], r["task_id"]): int(r["days"]) for r in day_rows}
    # First/last completed for span.
    span_rows = conn.execute(
        f"""
        SELECT project, task_id,
               MIN(finished_at) AS first_fin, MAX(finished_at) AS last_fin
        FROM runs WHERE status = 'completed'{proj_sql}
        GROUP BY project, task_id
        HAVING COUNT(*) >= ?
        """,
        [*params_base, MULTI_USE_MIN_COMPLETED],
    ).fetchall()
    span_map: dict[tuple[str, str], float] = {}
    for r in span_rows:
        try:
            a = datetime.fromisoformat(str(r["first_fin"]).replace("Z", ""))
            b = datetime.fromisoformat(str(r["last_fin"]).replace("Z", ""))
            span_map[(r["project"], r["task_id"])] = (b - a).total_seconds() / 86400.0
        except (TypeError, ValueError):
            continue

    multi: list[dict[str, Any]] = []
    flaky: list[dict[str, Any]] = []
    for r in rows:
        key = (str(r["project"]), str(r["task_id"]))
        completed = int(r["completed_run_count"] or 0)
        total = int(r["total_run_count"] or 0)
        window_days = days_map.get(key, 0)
        span = span_map.get(key, 0.0)
        is_multi = (completed >= MULTI_USE_MIN_COMPLETED and span >= MULTI_USE_SPAN_DAYS) or (
            window_days >= MULTI_USE_WINDOW_DAYS
        )
        item = {
            "project": key[0],
            "task_id": key[1],
            "status": r["status"],
            "completed_runs": completed,
            "total_runs": total,
            "completed_days_in_window": window_days,
            "completed_span_days": round(span, 1) if span else 0.0,
            "updated_at": r["updated_at"],
        }
        if is_multi:
            multi.append(item)
        elif (
            total >= FLAKY_MIN_TOTAL
            and completed <= 1
            or total >= FLAKY_MIN_TOTAL
            and completed > 0
            and (completed / total) < 0.4
        ):
            flaky.append(item)

    multi.sort(key=lambda x: (-x["completed_runs"], x["project"], x["task_id"]))
    flaky.sort(key=lambda x: (-x["total_runs"], x["project"], x["task_id"]))
    return multi, flaky


def _project_ranks(
    conn,
    *,
    since: str,
    archive_cutoff: str,
    proj_sql: str,
    params_base: list[Any],
    multi_keys: set[tuple[str, str]],
    older_than_days: float,
) -> list[dict[str, Any]]:
    totals = {
        str(r["project"]): int(r["n"])
        for r in conn.execute("SELECT project, COUNT(*) AS n FROM tasks GROUP BY project")
    }
    fleet = max(1, sum(totals.values()))
    completed = {
        str(r["project"]): int(r["n"])
        for r in conn.execute("SELECT project, COUNT(*) AS n FROM tasks WHERE status = 'completed' GROUP BY project")
    }
    disabled = {
        str(r["project"]): int(r["n"])
        for r in conn.execute("SELECT project, COUNT(*) AS n FROM tasks WHERE status = 'disabled' GROUP BY project")
    }
    runs_win = {
        str(r["project"]): int(r["n"])
        for r in conn.execute(
            "SELECT project, COUNT(*) AS n FROM runs WHERE started_at >= ? GROUP BY project",
            (since,),
        )
    }
    gpu = {}
    try:
        for r in conn.execute("SELECT project, gpu_seconds FROM project_usage"):
            gpu[str(r["project"])] = float(r["gpu_seconds"] or 0)
    except (sqlite3.Error, TypeError, ValueError):
        pass

    # Candidates for archive: completed + old enough.
    cand_sql = (
        "SELECT project, task_id, yaml_source, updated_at, "
        "COALESCE(completed_run_count, 0) AS completed_run_count "
        "FROM tasks WHERE status = 'completed' AND updated_at < ?"
    )
    cand_params: list[Any] = [archive_cutoff]
    if params_base:
        cand_sql += " AND project = ?"
        cand_params.extend(params_base)
    candidates = conn.execute(cand_sql, cand_params).fetchall()

    per_proj: dict[str, dict[str, Any]] = {}
    for name in totals:
        if params_base and name != params_base[0]:
            continue
        per_proj[name] = {
            "project": name,
            "task_count": totals.get(name, 0),
            "completed": completed.get(name, 0),
            "disabled": disabled.get(name, 0),
            "runs_in_window": runs_win.get(name, 0),
            "gpu_seconds": gpu.get(name, 0.0),
            "task_share": round(totals.get(name, 0) / fleet, 4),
            "archivable_count": 0,
            "skipped_shared_yaml_count": 0,
            "skipped_multi_use_count": 0,
            "sample_task_ids": [],
        }

    for row in candidates:
        name = str(row["project"])
        if name not in per_proj:
            continue
        tid = str(row["task_id"])
        key = (name, tid)
        bucket = per_proj[name]
        crc = int(row["completed_run_count"] or 0)
        if key in multi_keys or crc > 1:
            bucket["skipped_multi_use_count"] += 1
            continue
        src = row["yaml_source"] if "yaml_source" in row.keys() else None
        if not _movable(src):
            bucket["skipped_shared_yaml_count"] += 1
            continue
        bucket["archivable_count"] += 1
        if len(bucket["sample_task_ids"]) < ARCHIVE_SAMPLE_IDS:
            bucket["sample_task_ids"].append(tid)

    max_arch = max((p["archivable_count"] for p in per_proj.values()), default=1) or 1
    max_runs = max((p["runs_in_window"] for p in per_proj.values()), default=1) or 1
    out = []
    for p in per_proj.values():
        score = (
            0.5 * (p["archivable_count"] / max_arch) + 0.3 * p["task_share"] + 0.2 * (p["runs_in_window"] / max_runs)
        )
        p["overuse_score"] = round(score, 4)
        days_s = int(older_than_days) if float(older_than_days).is_integer() else older_than_days
        p["dry_run_command"] = (
            f"tq archive {p['project']} --older-than {days_s}d --status completed --max-completed-runs 1 --dry-run"
        )
        out.append(p)
    out.sort(key=lambda x: (-x["overuse_score"], -x["archivable_count"], x["project"]))
    return out


def _agent_prompt(payload: dict[str, Any]) -> str:
    churn = payload["churn"]
    lines = [
        f"Queue hygiene ({payload['days']}d since {payload['since']}): "
        f"{churn['tasks_added']} tasks added ({churn['tasks_added_per_day_avg']}/day), "
        f"{churn['runs_started']} runs started ({churn['runs_started_per_day_avg']}/day), "
        f"{churn['reruns_started']} re-runs.",
    ]
    if payload["multi_use"]:
        lines.append(
            f"Multi-use (keep): {len(payload['multi_use'])} tasks re-completed often "
            f"(e.g. {payload['multi_use'][0]['project']}/{payload['multi_use'][0]['task_id']})."
        )
    if payload["flaky"]:
        lines.append(f"Flaky (review, do not auto-archive): {len(payload['flaky'])} tasks.")
    props = payload["archive_proposals"]
    if props:
        lines.append("Archive proposals (dry-run only until the operator approves apply):")
        for p in props:
            lines.append(
                f"  - {p['project']}: archivable={p['archivable_count']} "
                f"(skip multi-use={p['skipped_multi_use_count']}, "
                f"shared YAML={p['skipped_shared_yaml_count']}) → `{p['dry_run_command']}`"
            )
        lines.append("Ask the operator before dropping --dry-run. Never auto-archive.")
    else:
        lines.append("No archive proposals (no stale one-shot completed tasks matched).")
    return "\n".join(lines)
