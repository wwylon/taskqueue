"""Cluster capacity snapshot for /api/capacity and MCP."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from taskqueue.config import PROTOCOL_VERSION
from taskqueue.resources import ResourceBudget, detect_system, ram_used_gib
from taskqueue.rows import row_get


def build_capacity(
    conn: sqlite3.Connection,
    cfg: dict[str, Any] | None = None,
    system: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from taskqueue.db import get_config

    if cfg is None:
        cfg = get_config(conn)
    # P-6: the web handler already holds the daemon's refreshed snapshot.
    # Copy so ram_used_gib does not mutate that shared dict.
    if system is None:
        system = detect_system()
    else:
        system = dict(system)
    system["ram_used_gib"] = round(ram_used_gib(), 1)
    budget = ResourceBudget(system, cfg)

    # Latest running run per task (prefer remote worker_id when present).
    running = conn.execute(
        """
        SELECT t.project, t.task_id, t.resources,
               (
                 SELECT r.worker_id FROM runs r
                 WHERE r.project = t.project AND r.task_id = t.task_id AND r.status = 'running'
                 ORDER BY r.run_id DESC LIMIT 1
               ) AS worker_id
        FROM tasks t
        WHERE t.status = 'running'
        """
    ).fetchall()
    reservations = []
    by_worker: dict[str, list[dict[str, float]]] = {}
    from taskqueue.rows import row_resources

    for row in running:
        res = row_resources(row)
        cpu = float(res.get("cpu_cores", 1))
        gpu = float(res.get("gpu_vram_gib", 0.0))
        ram = float(res.get("ram_gib", 0.0))
        wid = row["worker_id"]
        entry = {
            "project": row["project"],
            "task_id": row["task_id"],
            "cpu_cores": cpu,
            "ram_gib": ram,
            "gpu_vram_gib": gpu,
            "worker_id": wid,
        }
        reservations.append(entry)
        if wid:
            by_worker.setdefault(wid, []).append(entry)
        else:
            # Match daemon: live RAM/VRAM already seeded; only reserve CPU for locals.
            budget.reserve(cpu, 0.0, 0.0)

    # S9: sum VRAM declared by local (no worker_id) running tasks
    vram_gib_reserved_local = sum(
        float(row_resources(row).get("gpu_vram_gib", 0.0))
        for row in running
        if not row["worker_id"]
    )

    gpus = budget.gpus_free_snapshot()
    if not gpus and float(system.get("gpu_vram_total_gib") or 0.0) > 0:
        vram_total = float(system["gpu_vram_total_gib"])
        gpus = [{
            "index": 0,
            "vram_gib_total": vram_total,
            "vram_gib_free": max(0.0, round(budget.gpu_limit - budget.gpu_reserved, 2)),
            "vram_gib_reserved": round(vram_gib_reserved_local, 2),  # S9
        }]
    else:
        # S9: annotate existing GPU entries with vram_gib_reserved
        for g in gpus:
            g.setdefault("vram_gib_reserved", round(vram_gib_reserved_local, 2))
    # F-142: pack declared local VRAM onto devices so free is not optimistic vs reservations.
    if vram_gib_reserved_local > 0 and gpus:
        from taskqueue.resources import pack_gpu

        for row in running:
            if row["worker_id"]:
                continue
            need = float(row_resources(row).get("gpu_vram_gib", 0.0) or 0.0)
            if need <= 0:
                continue
            idx = pack_gpu(
                [{"index": g["index"], "vram_gib_free": g["vram_gib_free"]} for g in gpus],
                need,
            )
            if idx is None:
                continue
            for g in gpus:
                if int(g["index"]) == int(idx):
                    g["vram_gib_free"] = max(0.0, round(float(g["vram_gib_free"]) - need, 2))
                    break

    from taskqueue.db import worker_health
    from taskqueue.scheduler import parse_worker_load

    heartbeat_sec = float(cfg.get("heartbeat_sec", 30))
    workers_out = []
    for w in conn.execute("SELECT * FROM workers ORDER BY worker_id").fetchall():
        reserved = by_worker.get(w["worker_id"], [])
        cpu_decl = sum(r["cpu_cores"] for r in reserved)
        ram_decl = sum(r["ram_gib"] for r in reserved)
        vram_decl = sum(r["gpu_vram_gib"] for r in reserved)
        load = parse_worker_load(row_get(w, "load_json"))
        # Match claim path: free = total − max(live, declared reserved).
        if load:
            cpu_used = max(float(load.get("cpu_used", 0.0)), cpu_decl)
            ram_used = max(float(load.get("ram_used_gib", 0.0)), ram_decl)
            vram_used = max(float(load.get("vram_used_gib", 0.0)), vram_decl)
        else:
            cpu_used, ram_used, vram_used = cpu_decl, ram_decl, vram_decl
        online = w["status"] == "online"
        cpu_total = float(w["cpu_cores"] or 0)
        ram_total = float(w["ram_gib"] or 0)
        vram_total = float(w["gpu_vram_gib"] or 0)
        # Build per-device GPU entries from gpus_json; fall back to aggregate total.
        try:
            gpus_devices = json.loads(w["gpus_json"] if "gpus_json" in w.keys() else "[]") or []
        except Exception:
            gpus_devices = []
        if gpus_devices and online:
            worker_gpus = []
            for dev in gpus_devices:
                dev_total = float(dev.get("vram_gib", 0.0))
                dev_used = float(dev.get("vram_used_gib", 0.0))
                worker_gpus.append({
                    "index": int(dev.get("index", 0)),
                    "name": dev.get("name"),
                    "vram_gib_total": dev_total,
                    "vram_gib_free": max(0.0, round(dev_total - dev_used, 2)),
                })
        elif vram_total and online:
            worker_gpus = [{
                "index": 0,
                "vram_gib_total": vram_total,
                "vram_gib_free": max(0.0, round(vram_total - vram_used, 2)),
            }]
        else:
            worker_gpus = []
        workers_out.append({
            "worker_id": w["worker_id"],
            "status": w["status"],
            # Finer-grained than `status` (online/offline, used for dispatch
            # gating): "unreachable" means a heartbeat or two was missed but
            # the worker isn't past the stored-offline threshold yet -- a
            # plausible network blip, not yet a confirmed-dead worker.
            "health": worker_health(w["last_seen"], heartbeat_sec=heartbeat_sec),
            "cpu_cores_total": cpu_total,
            "cpu_cores_free": max(0.0, round(cpu_total - cpu_used, 2)) if online else 0,
            "ram_gib_free": max(0.0, round(ram_total - ram_used, 2)) if online else 0,
            "gpus": worker_gpus,
        })

    return {
        "protocol": PROTOCOL_VERSION,
        "master": {
            "cpu_cores_total": system["cpu_total_cores"],
            "cpu_cores_free": max(0.0, round(budget.cpu_limit - budget.cpu_reserved, 2)),
            "ram_gib_total": system["ram_total_gib"],
            "ram_gib_free": max(0.0, round(budget.ram_limit - budget.ram_reserved, 2)),
            "gpus": gpus,
            "gpu_probe_error": system.get("gpu_probe_error"),
            "gpu_probe_stale": bool(system.get("gpu_probe_stale")),
            "gpu_last_good_age_sec": system.get("gpu_last_good_age_sec") or 0.0,
            "gpu_processes": list(system.get("gpu_processes") or []),
            "limits_pct": {
                "cpu": cfg.get("cpu_limit_pct", 50),
                "gpu": cfg.get("gpu_limit_pct", 100),
                "ram": cfg.get("ram_limit_pct", 80),
                "ram_hard": cfg.get("ram_hard_limit_pct", 95),
            },
        },
        "workers": workers_out,
        "running_reservations": reservations,
    }
