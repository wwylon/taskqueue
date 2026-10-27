"""Shared per-tick caches for local admit and remote worker claim.

Both paths need frozen/paused/limits, service health, and resource estimates
without re-querying the same project for every candidate row.
"""
from __future__ import annotations

from typing import Any, Callable

from taskqueue.rows import row_resources, row_value


class AdmitProjectCaches:
    """Mutable caches keyed by project / (project, task) / services JSON."""

    __slots__ = ("frozen", "paused", "limits", "est", "svc")

    def __init__(self) -> None:
        self.frozen: dict[str, bool] = {}
        self.paused: dict[str, bool] = {}
        self.limits: dict[str, dict] = {}
        self.est: dict[tuple[str, str], dict] = {}
        self.svc: dict[str, str | None] = {}

    def project_state(
        self, conn: Any, proj_name: str
    ) -> tuple[bool, bool, dict]:
        from taskqueue.db import (
            get_project_limits,
            is_project_frozen,
            is_project_paused,
            with_gpu_usage,
        )

        if proj_name not in self.frozen:
            self.frozen[proj_name] = is_project_frozen(conn, proj_name)
        if proj_name not in self.paused:
            self.paused[proj_name] = is_project_paused(conn, proj_name)
        if proj_name not in self.limits:
            self.limits[proj_name] = with_gpu_usage(
                conn, proj_name, get_project_limits(conn, proj_name)
            )
        return self.frozen[proj_name], self.paused[proj_name], self.limits[proj_name]

    def estimate(
        self,
        conn: Any,
        project: str,
        task_id: str,
        res: dict,
        *,
        default_ram_gib: float = 1.0,
        predict_runs: int = 5,
        cache: Any = None,
    ) -> dict:
        from taskqueue.estimate import estimate_task_resources

        key = (project, task_id)
        if key not in self.est:
            self.est[key] = estimate_task_resources(
                conn,
                project,
                task_id,
                res,
                default_ram_gib=default_ram_gib,
                predict_runs=predict_runs,
                cache=cache,
            )
        return self.est[key]

    @staticmethod
    def svc_key(raw: Any) -> str:
        import json

        from taskqueue.jsonutil import json_list

        try:
            return json.dumps(json_list(raw if isinstance(raw, str) else None), sort_keys=True)
        except Exception:
            return str(raw or "[]")

    def services(
        self,
        svc_raw: Any,
        *,
        allow_remote: bool = False,
        probe_on_miss: bool = True,
        system_info: dict | None = None,
    ) -> tuple[str | None, str]:
        """Return ``(block_reason_or_None, code)``. Miss probes when ``probe_on_miss`` (F-138)."""
        from taskqueue.scheduler import check_services_cached

        sk = self.svc_key(svc_raw)
        if sk in self.svc:
            block = self.svc[sk]
            return block, ("" if block is None else "tq.block.service")
        if not probe_on_miss:
            return "service health unknown (cache miss)", "tq.block.service"
        block, code = check_services_cached(
            svc_raw if isinstance(svc_raw, str) else (svc_raw or "[]"),
            allow_remote=allow_remote,
            system_info=system_info,
        )
        self.svc[sk] = block
        return block, code or ""


def build_admit_context(
    row: Any,
    *,
    caches: AdmitProjectCaches,
    conn: Any,
    occupancy: dict,
    config: dict,
    dep_status: Callable[[str, str], str | None] | None,
    online_workers: int = 1,
    skip_node_capacity: bool = False,
    running_locks: set[str] | None = None,
    min_reserved_others: int = 0,
    allow_remote: bool = False,
    default_ram_gib: float = 1.0,
    predict_runs: int = 5,
    est_cache: Any = None,
    attempt_counts: dict[tuple[str, str], int] | None = None,
    system_info: dict | None = None,
) -> tuple[Any, float, float, float, int]:
    """Build ``FilterContext`` + estimated cpu/ram/gpu + failed-attempt count for one row.

    Used by remote claim; local admit uses the same caches for project/svc/estimate
    while keeping its ResourceBudget / unknown-footprint loop separate.
    """
    from taskqueue.db import failed_attempt_count
    from taskqueue.scheduler import build_filter_context

    proj = str(row["project"])
    task_id = str(row["task_id"])
    frozen, paused, limits = caches.project_state(conn, proj)
    force = bool(row["force_run"]) if "force_run" in row.keys() else False
    max_a = int(row["max_attempts"] or 0) if "max_attempts" in row.keys() else 0
    if max_a > 0:
        if attempt_counts is not None:
            attempts = int(attempt_counts.get((proj, task_id), 0))
        else:
            attempts = failed_attempt_count(conn, proj, task_id)
    else:
        attempts = 0
    res = row_resources(row)
    est = caches.estimate(
        conn,
        proj,
        task_id,
        res,
        default_ram_gib=default_ram_gib,
        predict_runs=predict_runs,
        cache=est_cache,
    )
    cpu = float(res.get("cpu_cores", 1))
    ram = float(est["ram_gib"])
    gpu = float(est["vram_gib"])
    svc_raw = row_value(row, "requires_services") or "[]"
    svc_block, svc_code = caches.services(
        svc_raw, allow_remote=allow_remote, probe_on_miss=True, system_info=system_info
    )
    ctx = build_filter_context(
        occupancy,
        config,
        frozen=frozen,
        paused=paused,
        project_limits=limits,
        svc_block=svc_block,
        svc_code=svc_code or "",
        dep_status=dep_status,
        online_workers=online_workers,
        force=force,
        skip_node_capacity=skip_node_capacity,
        running_locks=running_locks or set(),
        min_reserved_others=min_reserved_others,
    )
    return ctx, cpu, ram, gpu, attempts
