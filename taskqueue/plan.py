"""Read-only admission dry-run: which queued tasks the scheduler would start next.

Approximation of one scheduler tick: queue order is the real one (priority, fairshare),
dependency and project-pause gates are applied, then each candidate is charged its
*declared* resources against the live budget. Learned estimates, mutexes, services and
the P6 hold are not simulated, so a task shown as ``start`` may still be held back.
"""

from __future__ import annotations

import sqlite3
from typing import Any


def _gpu_budgets(conn: sqlite3.Connection, limits: dict[str, dict[str, float]]) -> dict[str, dict]:
    """Per-project daily GPU-hour budget: ``{project: {limit_hours, used_hours, remaining_hours}}``.

    Only projects with ``max_gpu_hours_day`` > 0 appear (same rule as the scheduler gate).
    """
    from taskqueue.db import gpu_hours_today

    out: dict[str, dict] = {}
    for proj, lim in limits.items():
        cap = float(lim.get("max_gpu_hours_day", 0) or 0)
        if cap <= 0:
            continue
        used = float(gpu_hours_today(conn, proj))
        out[proj] = {
            "limit_hours": cap,
            "used_hours": round(used, 3),
            "remaining_hours": round(max(0.0, cap - used), 3),
        }
    return out


def build_plan(
    conn: sqlite3.Connection,
    cfg: dict[str, Any] | None = None,
    system: dict[str, Any] | None = None,
    *,
    limit: int = 20,
) -> dict[str, Any]:
    from taskqueue.db import (
        blocked_projects, get_all_project_limits, get_config, get_queued_tasks,
        get_running_tasks, unmet_dependency_label,
    )
    from taskqueue.resources import ResourceBudget, detect_system, sort_queued_fairshare
    from taskqueue.scheduler import (
        build_task_status_map, declared_resources, occupancy_from_running_rows,
    )

    cfg = get_config(conn) if cfg is None else cfg
    budget = ResourceBudget(system if system is not None else detect_system(), cfg)
    status_map = build_task_status_map(conn)
    paused = set(blocked_projects(conn))
    running = get_running_tasks(conn)
    for row in running:
        cpu, ram, gpu = declared_resources(row)
        budget.reserve(cpu, gpu, ram)
    occ = occupancy_from_running_rows(running)
    usage = {
        r["project"]: float(r["gpu_seconds"])
        for r in conn.execute("SELECT project, gpu_seconds FROM project_usage").fetchall()
    }
    queued = sort_queued_fairshare(
        get_queued_tasks(conn, limit=max(limit * 4, 64), status_map=status_map,
                         skip_projects=paused),
        dict(occ["gpu_running_by_proj"]), usage, float(cfg.get("fairshare_weight", 1e-6)),
    )
    limits = get_all_project_limits(conn)
    gpu_budget = _gpu_budgets(conn, limits)
    started_by_proj: dict[str, int] = {}
    entries: list[dict[str, Any]] = []
    for row in queued[: max(1, int(limit))]:
        proj, tid = row["project"], row["task_id"]
        entry: dict[str, Any] = {"project": proj, "task_id": tid, "priority": row["priority"]}
        dep = unmet_dependency_label(conn, proj, tid, status_map=status_map, row=row)
        cap = int(float((limits.get(proj) or {}).get("max_tasks", 0) or 0))
        running_n = sum(1 for r in running if r["project"] == proj) + started_by_proj.get(proj, 0)
        if proj in paused:
            entry.update(verdict="blocked", reason="project paused or frozen")
        elif dep:
            entry.update(verdict="blocked", reason=f"waiting on {dep}")
        elif cap > 0 and running_n >= cap:
            entry.update(verdict="blocked", reason=f"project limit: {running_n}/{cap} running")
        else:
            cpu, ram, gpu = declared_resources(row)
            b = gpu_budget.get(proj)
            if gpu > 0 and b and b["remaining_hours"] <= 0:
                entry.update(
                    verdict="blocked",
                    reason=f"project max_gpu_hours_day={b['limit_hours']:g} reached "
                           f"({b['used_hours']:.2f} h used today, UTC)",
                )
                entries.append(entry)
                continue
            ok, why = budget.fits(cpu, gpu, ram)
            if ok:
                budget.reserve(cpu, gpu, ram)
                started_by_proj[proj] = started_by_proj.get(proj, 0) + 1
                entry.update(verdict="start", reason="")
            else:
                entry.update(verdict="blocked", reason=why)
        entries.append(entry)
    for n, e in enumerate((e for e in entries if e["verdict"] == "start"), 1):
        e["start_order"] = n
    return {
        "running": len(running),
        "paused_projects": sorted(paused),
        "would_start": sum(1 for e in entries if e["verdict"] == "start"),
        "approximate": True,
        "gpu_budget": gpu_budget,
        "tasks": entries,
    }
