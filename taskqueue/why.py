"""`tq why`: explain why a task is (not) running, from what the scheduler recorded plus a dry run.

The authoritative answer is the scheduler's last decision (``tasks.block_reason`` /
``last_error_code``, written each tick). The dry run (``plan.build_plan``) adds where the
task sits in the queue right now; it approximates one tick (see plan.py).
"""

from __future__ import annotations

import sqlite3
from typing import Any

_TERMINAL = ("completed", "failed", "interrupted", "cancelled")


def explain_task(conn: sqlite3.Connection, project: str, task_id: str, *, system: dict | None = None) -> dict[str, Any]:
    from taskqueue.db import blocked_projects, failed_attempt_count, get_task, unmet_dependency_label
    from taskqueue.plan import build_plan
    from taskqueue.rows import row_get

    row = get_task(conn, project, task_id)
    if row is None:
        return {"ok": False, "error": f"task not found: {project}/{task_id}"}
    status = str(row["status"])
    out: dict[str, Any] = {
        "ok": True,
        "project": project,
        "task_id": task_id,
        "status": status,
        "enabled": bool(row_get(row, "enabled", 1)),
        "block_reason": row_get(row, "block_reason"),
        "last_error_code": row_get(row, "last_error_code"),
        "reasons": [],
    }
    reasons: list[str] = out["reasons"]
    if status == "running":
        reasons.append("it is running")
        return out
    if not out["enabled"]:
        reasons.append("disabled (enabled: false) — enable it to queue it")
    if status in _TERMINAL:
        reasons.append(f"finished ({status}) — retry it to run again")
    if status == "suspended" or status == "stopped":
        reasons.append(f"{status} — it is re-queued when resources allow, or retry it")
    if project in set(blocked_projects(conn)):
        reasons.append("project is paused or frozen")
    dep = unmet_dependency_label(conn, project, task_id)
    if dep:
        reasons.append(f"waiting on dependency {dep}")
    max_a = int(row_get(row, "max_attempts", 0) or 0)
    if max_a > 0:
        n = failed_attempt_count(conn, project, task_id)
        out["attempts"] = {"failed": n, "max": max_a}
        if n >= max_a:
            reasons.append(f"max_attempts reached ({n}/{max_a})")
    if out["block_reason"]:
        reasons.append(f"scheduler: {out['block_reason']}")
    if status == "queued":
        plan = build_plan(conn, system=system, limit=200)
        entry = next((e for e in plan["tasks"] if e["project"] == project and e["task_id"] == task_id), None)
        if entry is None:
            out["plan"] = {"verdict": "outside window", "reason": "more than 200 tasks ahead in the queue"}
        else:
            out["plan"] = {k: entry[k] for k in ("verdict", "reason", "start_order") if k in entry}
            if entry["verdict"] == "start" and not reasons:
                reasons.append("nothing blocks it: it should start on the next scheduler tick")
            elif entry["verdict"] == "blocked" and entry.get("reason") and not out["block_reason"]:
                reasons.append(f"dry run: {entry['reason']}")
    if not reasons:
        reasons.append("no blocking condition found")
    return out


def format_explanation(info: dict[str, Any]) -> str:
    if not info.get("ok"):
        return str(info.get("error"))
    lines = [f"{info['project']}/{info['task_id']}: {info['status']}"]
    lines += [f"  - {r}" for r in info["reasons"]]
    plan = info.get("plan")
    if plan and not any(str(r).startswith("dry run") for r in info["reasons"]):
        extra = f" #{plan['start_order']}" if plan.get("start_order") else ""
        lines.append(f"  dry run: {plan['verdict']}{extra}" + (f" ({plan['reason']})" if plan.get("reason") else ""))
    return "\n".join(lines)
