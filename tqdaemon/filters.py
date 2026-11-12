"""Nodes and hard filters shared by local admission and remote claims (split from scheduler.py).

Pure checks: a candidate row + a Node + a FilterContext -> FilterResult. scheduler.py re-exports
every name for existing callers.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from tqdaemon import errors as tqerr
from tqdaemon.db import (
    is_project_paused,
)
from tqdaemon.jsonutil import json_list
from tqdaemon.models import (
    STATUS_COMPLETED,
)
from tqdaemon.resources import (
    remote_worker_gpu_fits,
)
from tqdaemon.rows import row_value

MASTER_NODE_ID = "master-local"


def _version_matches(actual: str, constraint: str) -> bool:
    """Version comparison: >=, >, <=, <, ==, !=, bare X.Y (means >=); comma = AND."""
    import re

    parts = [c.strip() for c in str(constraint).split(",") if c.strip()]
    if len(parts) > 1:
        return all(_version_matches(actual, c) for c in parts)
    if not parts:
        return True
    m = re.match(r"^([><=!]*)\s*(\d[\d.]*)$", parts[0])
    if not m:
        return True  # unknown constraint format - pass
    op, ver_str = m.group(1), m.group(2)

    def _parse(v: str) -> tuple:
        return tuple(int(x) for x in v.split(".") if x.isdigit())

    try:
        actual_t = _parse(actual.split()[0])  # "3.11.0 (main, ...)" -> "3.11.0"
        req_t = _parse(ver_str)
        n = max(len(actual_t), len(req_t))
        actual_t += (0,) * (n - len(actual_t))
        req_t += (0,) * (n - len(req_t))
        if op in ("", ">="):
            return actual_t >= req_t
        if op == "==":
            return actual_t == req_t
        if op == ">":
            return actual_t > req_t
        if op == "<=":
            return actual_t <= req_t
        if op == "<":
            return actual_t < req_t
        if op == "!=":
            return actual_t != req_t
    except (AttributeError, IndexError, TypeError, ValueError):
        pass
    return True


def should_schedule_locally(dispatch: str, *, online_workers: int = 0, local_running: int = 0) -> bool:
    """Whether the master may start this task locally.

    ``dispatch: remote`` is a hint: prefer workers when any are online; if none
    are online, fall back to local scheduling.

    ``dispatch: remote-only-if-busy`` (F-125): prefer remote when the local host
    already has running work *and* workers are online; otherwise run locally.
    """
    d = (dispatch or "any").strip()
    if d == "remote":
        return int(online_workers) <= 0
    if d == "remote-only-if-busy":
        if int(online_workers) > 0 and int(local_running) > 0:
            return False
        return True
    return True


@dataclass
class Node:
    """A schedulable machine: the local master or one remote worker."""

    node_id: str
    os: str = "any"
    arch: str = "any"
    ram_gib: float = 0.0
    cpu_cores: float = 0.0
    gpu_vram_gib: float = 0.0
    gpus: list[dict[str, Any]] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    projects: list[str] | None = None  # None = all projects (local master)
    is_local: bool = False
    ram_used_gib: float = 0.0
    vram_used_gib: float = 0.0
    cpu_used: float = 0.0
    draining: bool = False
    python_version: str = ""
    cuda_version: str = ""
    trust: str = "trusted"  # C2: worker trust tier
    stable: bool = False  # Q2: operator-declared stable worker (TQ_WORKER_STABLE=1)

    @property
    def ram_free_gib(self) -> float:
        return max(0.0, float(self.ram_gib) - float(self.ram_used_gib))

    @property
    def cpu_free(self) -> float:
        return max(0.0, float(self.cpu_cores) - float(self.cpu_used))

    @property
    def vram_free_gib(self) -> float:
        return max(0.0, float(self.gpu_vram_gib) - float(self.vram_used_gib))


@dataclass(frozen=True)
class FilterResult:
    ok: bool
    reason: str = ""
    code: str = ""
    silent: bool = False

    @staticmethod
    def accept() -> FilterResult:
        return FilterResult(True)

    @staticmethod
    def reject(reason: str, code: str = "", *, silent: bool = False) -> FilterResult:
        return FilterResult(False, reason, code, silent)


@dataclass
class FilterContext:
    """Occupancy + policy snapshot for one Filter pass against a Node."""

    n_running: int = 0
    has_exclusive: bool = False
    exclusive_task: str = ""  # project/task_id of the running exclusive task
    # project -> project/task_id of a running exclusive_project holder
    exclusive_projects: dict[str, str] = field(default_factory=dict)
    n_running_by_proj: dict[str, int] = field(default_factory=dict)
    cpu_by_proj: dict[str, float] = field(default_factory=dict)
    ram_by_proj: dict[str, float] = field(default_factory=dict)
    gpu_by_proj: dict[str, float] = field(default_factory=dict)
    total_cpu_declared: float = 0.0
    total_ram_declared: float = 0.0
    frozen: bool = False
    paused: bool = False
    project_limits: dict[str, Any] = field(default_factory=dict)
    g_max_tasks: int = 0
    # T-205: slots held back for other projects' unmet ``min_tasks``.
    min_reserved_others: int = 0
    g_max_cpu: float = 0.0
    g_max_ram: float = 0.0
    online_workers: int = 0
    svc_block: str | None = None
    svc_code: str = ""
    dep_status: Callable[[str, str], str | None] | None = None
    force: bool = False
    skip_node_capacity: bool = False
    running_locks: set[str] = field(default_factory=set)
    backoff_left_sec: float = 0.0  # retry_backoff_sec still to wait (tqdaemon.backoff)
    # Subtree total reached in an ancestor project set (set_caps.SetCapChecker), else None.
    set_block: str | None = None


def build_filter_context(
    occupancy: dict,
    config: dict,
    *,
    frozen: bool = False,
    paused: bool = False,
    project_limits: dict | None = None,
    svc_block: str | None = None,
    svc_code: str = "",
    dep_status: Callable[[str, str], str | None] | None = None,
    online_workers: int = 1,
    force: bool = False,
    skip_node_capacity: bool = False,
    running_locks: set[str] | None = None,
    min_reserved_others: int = 0,
    backoff_left_sec: float = 0.0,
    set_block: str | None = None,
) -> FilterContext:
    """Construct a FilterContext from occupancy + config dicts.

    Both the local scheduler and worker_api build the same struct from these
    inputs; callers supply the project-level lookups (frozen/paused/limits) and
    the dep_status callable that fits their connection context.
    """
    return FilterContext(
        n_running=occupancy["n_running"],
        has_exclusive=occupancy["has_exclusive"],
        exclusive_task=occupancy.get("exclusive_task") or "",
        exclusive_projects=occupancy.get("exclusive_projects") or {},
        n_running_by_proj=occupancy["n_running_by_proj"],
        cpu_by_proj=occupancy["cpu_by_proj"],
        ram_by_proj=occupancy["ram_by_proj"],
        gpu_by_proj=occupancy["gpu_by_proj"],
        total_cpu_declared=occupancy["total_cpu_declared"],
        total_ram_declared=occupancy["total_ram_declared"],
        frozen=frozen,
        paused=paused,
        project_limits=project_limits or {},
        g_max_tasks=int(config.get("max_running_tasks") or 0),
        g_max_cpu=float(config.get("max_cpu_cores") or 0),
        g_max_ram=float(config.get("max_ram_gib") or 0),
        online_workers=online_workers,
        svc_block=svc_block,
        svc_code=svc_code,
        dep_status=dep_status,
        force=force,
        skip_node_capacity=skip_node_capacity,
        running_locks=running_locks or set(),
        min_reserved_others=min_reserved_others,
        backoff_left_sec=backoff_left_sec,
        set_block=set_block,
    )


_row_get = row_value


def _norm_arch(arch: str) -> str:
    a = (arch or "").lower()
    if a in ("x86_64", "amd64"):
        return "x86_64"
    return a


def _parse_resources(raw: Any) -> dict:
    """Parse a task ``resources`` JSON column once per distinct value (callers must not mutate)."""
    from tqdaemon.rows import _parse_resources_str, row_resources

    if isinstance(raw, dict):
        return raw
    if hasattr(raw, "keys") and not isinstance(raw, (str, bytes)):
        return row_resources(raw)
    return _parse_resources_str(raw if isinstance(raw, str) else "")


def declared_resources(row: Any) -> tuple[float, float, float]:
    from tqdaemon.rows import row_resources

    res = row_resources(row)
    return _num(res.get("cpu_cores"), 1.0), _num(res.get("ram_gib"), 0.0), _num(res.get("gpu_vram_gib"), 0.0)


def _num(value: Any, default: float) -> float:
    """Resource field as float; None / junk (e.g. ``cpu_cores: null`` in YAML) -> default.
    One bad row must never raise inside the admit loop and stall every project."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def node_from_system(system_info: dict[str, Any], *, node_id: str = MASTER_NODE_ID) -> Node:
    os_info = system_info.get("os") or {}
    if isinstance(os_info, dict):
        os_name = str(os_info.get("system") or "any").lower()
        arch = str(os_info.get("machine") or "any").lower()
    else:
        os_name = str(os_info or "any").lower()
        arch = "any"
    gpus = list(system_info.get("gpus") or [])
    ram_total = float(system_info.get("ram_total_gib") or system_info.get("ram_gib") or 0.0)
    ram_used = float(system_info.get("ram_used_gib") or 0.0)
    vram_total = float(system_info.get("gpu_vram_total_gib") or system_info.get("gpu_vram_gib") or 0.0)
    return Node(
        node_id=node_id,
        os=os_name,
        arch=arch,
        ram_gib=ram_total,
        cpu_cores=float(system_info.get("cpu_total_cores") or system_info.get("cpu_cores") or 0.0),
        gpu_vram_gib=vram_total,
        gpus=gpus,
        tags=list(system_info.get("tags") or []),
        projects=None,
        is_local=True,
        ram_used_gib=ram_used,
        vram_used_gib=float(system_info.get("vram_used_gib") or 0.0),
        cpu_used=float(system_info.get("cpu_used") or 0.0),
    )


def parse_worker_load(raw: Any) -> dict[str, Any]:
    """Normalize heartbeat live-load (R-008) to ram/vram/cpu used (+ optional gpu_free).

    F-139: workers also send per-device ``gpu_free``; keep it so placement can
    refresh ``gpus[].vram_used_gib`` without waiting for the next registration.
    """
    if not raw:
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for key in ("ram_used_gib", "vram_used_gib", "cpu_used"):
        if raw.get(key) is not None:
            try:
                out[key] = float(raw[key])
            except (TypeError, ValueError):
                pass
    gf = raw.get("gpu_free")
    if isinstance(gf, list):
        cleaned: list[dict[str, float]] = []
        for item in gf:
            if not isinstance(item, dict):
                cleaned.append({})
                continue
            try:
                cleaned.append({"vram_free_gib": float(item.get("vram_free_gib", 0.0))})
            except (TypeError, ValueError):
                cleaned.append({})
        out["gpu_free"] = cleaned
    return out


def _merge_gpu_free_into_gpus(gpus: list[dict[str, Any]], gpu_free: list[Any] | None) -> list[dict[str, Any]]:
    """F-139: overlay heartbeat per-device free VRAM onto registration GPU inventory."""
    if not gpus or not isinstance(gpu_free, list):
        return list(gpus or [])
    out: list[dict[str, Any]] = []
    for i, g in enumerate(gpus):
        ng = dict(g)
        if i < len(gpu_free) and isinstance(gpu_free[i], dict):
            try:
                free = float(gpu_free[i].get("vram_free_gib", 0.0))
                total = float(ng.get("vram_gib", 0.0) or 0.0)
                ng["vram_used_gib"] = max(0.0, total - free)
                ng["vram_gib_free"] = max(0.0, free)
            except (TypeError, ValueError):
                pass
        out.append(ng)
    return out


def node_from_worker(
    worker: Any,
    *,
    reserved_cpu: float = 0.0,
    reserved_ram: float = 0.0,
    reserved_vram: float = 0.0,
    load: dict[str, Any] | None = None,
) -> Node:
    """Build a Node from a workers-table row, applying live load when present."""
    try:
        gpus = json_list(_row_get(worker, "gpus_json", "[]"))
    except (json.JSONDecodeError, TypeError):
        gpus = []
    try:
        tags = json_list(_row_get(worker, "tags", "[]"))
    except (json.JSONDecodeError, TypeError):
        tags = []
    try:
        projects = json_list(_row_get(worker, "projects", "[]"))
    except (json.JSONDecodeError, TypeError):
        projects = []
    load = load if load is not None else parse_worker_load(_row_get(worker, "load_json", None))
    ram_used = float(load.get("ram_used_gib", reserved_ram) if load else reserved_ram)
    vram_used = float(load.get("vram_used_gib", reserved_vram) if load else reserved_vram)
    cpu_used = float(load.get("cpu_used", reserved_cpu) if load else reserved_cpu)
    if load:
        ram_used = max(ram_used, reserved_ram)
        vram_used = max(vram_used, reserved_vram)
        cpu_used = max(cpu_used, reserved_cpu)
        gpus = _merge_gpu_free_into_gpus(list(gpus or []), load.get("gpu_free"))
    else:
        gpus = list(gpus or [])
    return Node(
        node_id=str(_row_get(worker, "worker_id", "")),
        os=str(_row_get(worker, "os", "any") or "any").lower(),
        arch=str(_row_get(worker, "arch", "any") or "any").lower(),
        ram_gib=float(_row_get(worker, "ram_gib", 0.0) or 0.0),
        cpu_cores=float(_row_get(worker, "cpu_cores", 0) or 0.0),
        gpu_vram_gib=float(_row_get(worker, "gpu_vram_gib", 0.0) or 0.0),
        gpus=gpus,
        tags=list(tags or []),
        projects=list(projects or []),
        is_local=False,
        ram_used_gib=ram_used,
        vram_used_gib=vram_used,
        cpu_used=cpu_used,
        draining=str(_row_get(worker, "status", "") or "") == "draining",
        trust=str(_row_get(worker, "trust", "trusted") or "trusted"),
        stable=bool(_row_get(worker, "stable", False)),
    )


def node_matches_requires(node: Node, requires_str: str) -> bool:
    try:
        req = json.loads(requires_str or "{}")
    except (TypeError, ValueError):
        return True
    if not req:
        return True

    if "os" in req:
        target_os = str(req["os"]).lower()
        if target_os not in ("any", "") and node.os not in ("any", ""):
            if target_os != node.os.lower():
                return False

    if "arch" in req:
        target_arch = _norm_arch(str(req["arch"]))
        node_arch = _norm_arch(node.arch)
        if target_arch not in ("any", "") and node_arch not in ("any", ""):
            if target_arch != node_arch:
                return False

    min_ram = req.get("min_ram_gib", req.get("ram_gib"))
    if min_ram is not None and node.ram_gib < float(min_ram):
        return False

    if "gpu" in req:
        gpus = node.gpus or []
        try:
            req_vram = float(req["gpu"])
            if req_vram > 0.0:
                if gpus:
                    max_single = max((float(g.get("vram_gib", 0.0)) for g in gpus), default=0.0)
                    if max_single < req_vram:
                        return False
                elif node.gpu_vram_gib < req_vram:
                    return False
        except (TypeError, ValueError):
            if req["gpu"] and not gpus and node.gpu_vram_gib <= 0:
                return False

    req_tags = req.get("tags") or []
    if req_tags:
        if node.is_local and not node.tags:
            pass
        elif any(t not in node.tags for t in req_tags):
            return False

    if "python" in req:
        req_py = str(req["python"]).strip()
        node_py = str(getattr(node, "python_version", "") or "").strip()
        if req_py and node_py:
            if not _version_matches(node_py, req_py):
                return False

    if "cuda" in req:
        req_cuda = str(req["cuda"]).strip()
        node_cuda = str(getattr(node, "cuda_version", "") or "").strip()
        if req_cuda and node_cuda:
            if not _version_matches(node_cuda, req_cuda):
                return False

    if "shell" in req:
        req_shell = str(req["shell"]).lower().strip()
        node_os = node.os.lower()
        if req_shell in ("cmd", "pwsh"):
            if node_os not in ("windows", "any", ""):
                return False
        elif req_shell in ("sh", "bash"):
            if node_os in ("windows",):
                return False

    return True


def filter_project_allowed(row: Any, node: Node) -> FilterResult:
    if node.projects is None:
        return FilterResult.accept()
    proj = str(_row_get(row, "project", ""))
    if proj not in node.projects:
        return FilterResult.reject("project not allowed on this node", silent=True)
    return FilterResult.accept()


def filter_dispatch(row: Any, node: Node, ctx: FilterContext) -> FilterResult:
    dispatch = _row_get(row, "dispatch", "any") or "any"
    if node.is_local:
        if not should_schedule_locally(
            dispatch,
            online_workers=ctx.online_workers,
            local_running=ctx.n_running,
        ):
            return FilterResult.reject("dispatch prefers remote workers", silent=True)
        return FilterResult.accept()
    if dispatch not in ("remote", "any", "remote-only-if-busy"):
        return FilterResult.reject("dispatch is local-only", silent=True)
    return FilterResult.accept()


def filter_requires(row: Any, node: Node) -> FilterResult:
    requires_str = _row_get(row, "requires", "{}") or "{}"
    if node_matches_requires(node, requires_str):
        return FilterResult.accept()
    # Build a human-readable mismatch summary.
    try:
        req = json.loads(requires_str)
    except (TypeError, ValueError):
        req = {}
    parts: list[str] = []
    if "os" in req and req["os"] not in ("any", ""):
        parts.append(f"os={req['os']} (node: {node.os})")
    if "arch" in req and req["arch"] not in ("any", ""):
        parts.append(f"arch={req['arch']} (node: {node.arch})")
    min_ram = req.get("min_ram_gib", req.get("ram_gib"))
    if min_ram is not None and node.ram_gib < float(min_ram):
        parts.append(f"min_ram_gib={min_ram} (node: {node.ram_gib:.1f})")
    if not parts:
        parts.append("requires not matched")
    reason = "no matching node for requires: " + ", ".join(parts)
    return FilterResult.reject(reason)


def filter_freeze_pause(ctx: FilterContext) -> FilterResult:
    if ctx.frozen:
        return FilterResult.reject("project frozen", silent=True)
    if ctx.paused:
        return FilterResult.reject("project paused", silent=True)
    return FilterResult.accept()


def filter_draining(node: Node) -> FilterResult:
    if node.draining:
        return FilterResult.reject("worker draining", silent=True)
    return FilterResult.accept()


# C2: trust-tier ordering — higher rank = more privileged
_TRUST_RANK: dict[str, int] = {"trusted": 3, "lab": 2, "spot": 1, "untrusted": 0, "": 3}


def filter_trust(row: Any, node: Node) -> FilterResult:
    """C2: reject if the worker's trust tier is below the task's min_trust."""
    min_trust = str(_row_get(row, "min_trust", "") or "").strip().lower()
    if not min_trust:
        return FilterResult.accept()
    node_trust = str(getattr(node, "trust", "trusted") or "trusted").lower()
    if _TRUST_RANK.get(node_trust, 3) < _TRUST_RANK.get(min_trust, 0):
        return FilterResult.reject(
            f"worker trust '{node_trust}' < required '{min_trust}'",
            silent=node.is_local,
        )
    return FilterResult.accept()


def filter_services(ctx: FilterContext) -> FilterResult:
    if ctx.svc_block:
        return FilterResult.reject(ctx.svc_block, ctx.svc_code or tqerr.BLOCK_SERVICE)
    return FilterResult.accept()


def filter_exclusive(row: Any, ctx: FilterContext) -> FilterResult:
    exclusive = bool(_row_get(row, "exclusive", False))
    if exclusive and ctx.n_running > 0:
        return FilterResult.reject(
            "waiting for other tasks to finish before exclusive run",
            tqerr.BLOCK_EXCLUSIVE,
        )
    if ctx.has_exclusive:
        blocker = f" ({ctx.exclusive_task})" if ctx.exclusive_task else ""
        return FilterResult.reject(
            f"blocked while an exclusive task is running{blocker}",
            tqerr.BLOCK_EXCLUSIVE,
        )
    proj = str(_row_get(row, "project", ""))
    exclusive_project = bool(_row_get(row, "exclusive_project", False))
    n_proj = int(ctx.n_running_by_proj.get(proj, 0) or 0)
    if exclusive_project and n_proj > 0:
        return FilterResult.reject(
            "waiting for other project tasks to finish before exclusive_project run",
            tqerr.BLOCK_EXCLUSIVE_PROJECT,
        )
    if proj and proj in ctx.exclusive_projects:
        blocker = ctx.exclusive_projects[proj]
        suffix = f" ({blocker})" if blocker else ""
        return FilterResult.reject(
            f"blocked while an exclusive_project task is running{suffix}",
            tqerr.BLOCK_EXCLUSIVE_PROJECT,
        )
    return FilterResult.accept()


def filter_max_attempts(row: Any, attempt_count: int) -> FilterResult:
    max_a = int(_row_get(row, "max_attempts", 0) or 0)
    if max_a > 0 and attempt_count >= max_a:
        return FilterResult.reject(
            f"max_attempts={max_a} reached — marked failed",
            tqerr.BLOCK_MAX_ATTEMPTS,  # F-099: was incorrectly RUN_NONZERO (a run-outcome code)
        )
    return FilterResult.accept()


def filter_global_limits(ctx: FilterContext, *, cpu: float, ram: float) -> FilterResult:
    if ctx.g_max_tasks > 0 and ctx.n_running >= ctx.g_max_tasks:
        return FilterResult.reject(
            f"max_running_tasks={ctx.g_max_tasks} reached ({ctx.n_running} running)",
            tqerr.BLOCK_RESOURCES,
        )
    if ctx.force:
        return FilterResult.accept()
    if ctx.g_max_cpu > 0 and ctx.total_cpu_declared + cpu > ctx.g_max_cpu:
        return FilterResult.reject(
            f"global max_cpu_cores={ctx.g_max_cpu:.0f} reached ({ctx.total_cpu_declared:.0f} in use)",
            tqerr.BLOCK_RESOURCES,
        )
    if ctx.g_max_ram > 0 and ctx.total_ram_declared + ram > ctx.g_max_ram:
        return FilterResult.reject(
            f"global max_ram_gib={ctx.g_max_ram:.1f} reached ({ctx.total_ram_declared:.1f} in use)",
            tqerr.BLOCK_RESOURCES,
        )
    return FilterResult.accept()


def reserved_slots_for_others(
    all_limits: dict[str, dict[str, Any]],
    queued_by_proj: dict[str, int],
    running_by_proj: dict[str, int],
    proj: str,
) -> int:
    """T-205: slots other projects are still owed by their ``min_tasks``.

    A project only reserves while it has queued work, so an idle project never
    hoards slots. Unmet = ``max(0, min_tasks - running)``.
    """
    total = 0
    for other, lim in all_limits.items():
        if other == proj or queued_by_proj.get(other, 0) <= 0:
            continue
        want = int(float(lim.get("min_tasks", 0) or 0))
        if want > 0:
            total += max(0, want - running_by_proj.get(other, 0))
    return total


def filter_project_min(ctx: FilterContext) -> FilterResult:
    """T-205: keep ``max_running_tasks`` slots free for projects below ``min_tasks``."""
    if ctx.force or ctx.g_max_tasks <= 0 or ctx.min_reserved_others <= 0:
        return FilterResult.accept()
    if ctx.n_running + ctx.min_reserved_others >= ctx.g_max_tasks:
        return FilterResult.reject(
            f"slots reserved for other projects' min_tasks "
            f"({ctx.min_reserved_others} held, {ctx.n_running}/{ctx.g_max_tasks} running)",
            tqerr.BLOCK_RESOURCES,
        )
    return FilterResult.accept()


def filter_project_limits(row: Any, ctx: FilterContext, *, cpu: float, ram: float, gpu: float) -> FilterResult:
    proj = str(_row_get(row, "project", ""))
    plim = ctx.project_limits or {}
    p_max_tasks = int(plim.get("max_tasks", 0) or 0)
    p_running = ctx.n_running_by_proj.get(proj, 0)
    if p_max_tasks > 0 and p_running >= p_max_tasks:
        return FilterResult.reject(
            f"project max_tasks={p_max_tasks:.0f} reached ({p_running} running)",
            tqerr.BLOCK_RESOURCES,
        )
    if ctx.force:
        return FilterResult.accept()
    p_gpu_day = float(plim.get("max_gpu_hours_day", 0) or 0)
    if gpu > 0 and p_gpu_day > 0 and float(plim.get("gpu_hours_today", 0) or 0) >= p_gpu_day:
        return FilterResult.reject(
            f"project max_gpu_hours_day={p_gpu_day:g} reached "
            f"({float(plim.get('gpu_hours_today', 0)):.2f} h used today, UTC)",
            tqerr.BLOCK_RESOURCES,
        )
    p_max_cpu = float(plim.get("max_cpu_cores", 0) or 0)
    p_max_ram = float(plim.get("max_ram_gib", 0) or 0)
    p_max_gpu = float(plim.get("max_gpu_vram_gib", 0) or 0)
    p_cpu = ctx.cpu_by_proj.get(proj, 0.0)
    p_ram = ctx.ram_by_proj.get(proj, 0.0)
    p_gpu = ctx.gpu_by_proj.get(proj, 0.0)
    if p_max_cpu > 0 and p_cpu + cpu > p_max_cpu:
        return FilterResult.reject(
            f"project max_cpu_cores={p_max_cpu:.0f} reached ({p_cpu:.0f} in use)",
            tqerr.BLOCK_RESOURCES,
        )
    if p_max_ram > 0 and p_ram + ram > p_max_ram:
        return FilterResult.reject(
            f"project max_ram_gib={p_max_ram:.1f} reached ({p_ram:.1f} in use)",
            tqerr.BLOCK_RESOURCES,
        )
    if p_max_gpu > 0 and p_gpu + gpu > p_max_gpu:
        return FilterResult.reject(
            f"project max_gpu_vram_gib={p_max_gpu:.1f} reached ({p_gpu:.1f} in use)",
            tqerr.BLOCK_RESOURCES,
        )
    return FilterResult.accept()


def build_set_checker(conn: sqlite3.Connection, set_paths: dict[str, str | None]):
    """One SetCapChecker per tick/claim (empty -> falsy, so callers skip the work)."""
    from tqdaemon.db import gpu_hours_today
    from tqdaemon.project_limits import get_all_set_limits
    from tqdaemon.set_caps import SetCapChecker

    return SetCapChecker(get_all_set_limits(conn), set_paths, gpu_hours_today=lambda p: gpu_hours_today(conn, p))


def filter_set_caps(ctx: FilterContext) -> FilterResult:
    """Ancestor project-set totals (``bots`` max_tasks=4 = 4 across every bot project)."""
    if ctx.set_block and not ctx.force:
        return FilterResult.reject(ctx.set_block, tqerr.BLOCK_RESOURCES)
    return FilterResult.accept()


def filter_deps(row: Any, ctx: FilterContext) -> FilterResult:
    from tqdaemon.queries import DEAD_DEP_STATUSES, resolve_dep_ref

    deps = json_list(_row_get(row, "depends_on", "[]"))
    deps_any = json_list(_row_get(row, "depends_on_any", "[]"))
    if not deps and not deps_any:
        return FilterResult.accept()
    lookup = ctx.dep_status
    if lookup is None:
        return FilterResult.accept()
    proj = str(_row_get(row, "project", ""))

    for dep_ref in deps:
        dep_proj, dep_id, dep_label = resolve_dep_ref(proj, str(dep_ref))
        dep_status = lookup(dep_proj, str(dep_id))
        if dep_status is None:
            return FilterResult.reject(f"waiting on: {dep_label} (not found)", tqerr.BLOCK_DEPS)
        # F-090: include disabled (matches queries.DEAD_DEP_STATUSES / health UI).
        if dep_status in DEAD_DEP_STATUSES:
            return FilterResult.reject(
                f"dead dependency: {dep_label} [{dep_status}] — retry or remove it to unblock",
                tqerr.BLOCK_DEPS,
            )
        if dep_status != STATUS_COMPLETED:
            return FilterResult.reject(
                f"waiting on: {dep_label} [{dep_status}]",
                tqerr.BLOCK_DEPS,
            )

    if deps_any:
        any_ok = False
        waiting_labels: list[str] = []
        dead_labels: list[str] = []
        for dep_ref in deps_any:
            dep_proj, dep_id, dep_label = resolve_dep_ref(proj, str(dep_ref))
            dep_status = lookup(dep_proj, str(dep_id))
            if dep_status == STATUS_COMPLETED:
                any_ok = True
                break
            if dep_status is None:
                waiting_labels.append(f"{dep_label} (not found)")
            elif dep_status in DEAD_DEP_STATUSES:
                dead_labels.append(f"{dep_label} [{dep_status}]")
            else:
                waiting_labels.append(f"{dep_label} [{dep_status}]")
        if not any_ok:
            if waiting_labels:
                return FilterResult.reject(
                    f"waiting on any of: {', '.join(waiting_labels)}",
                    tqerr.BLOCK_DEPS,
                )
            return FilterResult.reject(
                f"dead dependency (any): {', '.join(dead_labels)} — retry or remove one to unblock",
                tqerr.BLOCK_DEPS,
            )
    return FilterResult.accept()


def pick_reservation_top(
    conn: sqlite3.Connection, queued: list, dep_lookup: Callable[[str, str], str | None]
) -> Any | None:
    """First queued task that is not blocked for a non-capacity reason.

    A dependency-blocked or paused-project task must never hold RAM/VRAM (P6), otherwise it
    starves every runnable task behind it.
    """
    ctx = FilterContext(dep_status=dep_lookup)
    paused: dict[str, bool] = {}
    for row in queued:
        proj = str(row["project"])
        if proj not in paused:
            paused[proj] = is_project_paused(conn, proj)
        if paused[proj]:
            continue
        if not filter_deps(row, ctx).ok:
            continue
        return row
    return None


def exclusive_drain_target(
    top: Any | None, n_running: int, n_running_by_proj: dict[str, int]
) -> tuple[tuple[str, str], str | None] | None:
    """Return ``(top_key, scope_project)`` when admission must drain for an exclusive task.

    The top runnable task is exclusive and cannot start yet: letting lower-priority
    tasks in would keep ``n_running > 0`` forever. ``scope_project`` is None for a global
    ``exclusive`` task (drain everything) or the project name for ``exclusive_project``.
    """
    if top is None:
        return None
    key = (str(top["project"]), str(top["task_id"]))
    if bool(_row_get(top, "exclusive", False)) and n_running > 0:
        return key, None
    proj = str(top["project"])
    if bool(_row_get(top, "exclusive_project", False)) and int(n_running_by_proj.get(proj, 0) or 0) > 0:
        return key, proj
    return None


def filter_mutex(row: Any, ctx: FilterContext) -> FilterResult:
    """Named resource locks — block if any lock is held by a running task."""
    res = _parse_resources(_row_get(row, "resources", "{}"))
    task_locks = {str(x) for x in (res.get("locks") or [])}
    if not task_locks:
        return FilterResult.accept()
    held = sorted(task_locks & ctx.running_locks)
    if held:
        return FilterResult.reject(
            f"lock(s) held by another running task: {', '.join(held)}",
            tqerr.BLOCK_MUTEX,
        )
    return FilterResult.accept()


def filter_node_capacity(node: Node, ctx: FilterContext, *, cpu: float, ram: float, gpu: float) -> FilterResult:
    if ctx.skip_node_capacity or ctx.force:
        return FilterResult.accept()
    if cpu > 0 and node.cpu_free + 1e-9 < cpu:
        return FilterResult.reject(
            f"node {node.node_id}: need {cpu:.0f} CPU cores, {node.cpu_free:.0f} free",
            tqerr.BLOCK_RESOURCES,
        )
    if ram > 0 and node.ram_free_gib + 1e-9 < ram:
        return FilterResult.reject(
            f"node {node.node_id}: need {ram:.1f} GiB RAM, {node.ram_free_gib:.1f} free",
            tqerr.BLOCK_RESOURCES,
        )
    if gpu > 0:
        if node.gpus:
            if not remote_worker_gpu_fits(node.gpus, gpu):
                return FilterResult.reject(
                    f"node {node.node_id}: no GPU with {gpu:.1f} GiB VRAM free",
                    tqerr.BLOCK_VRAM_EXCEEDS_SYSTEM,
                )
        elif node.vram_free_gib + 1e-9 < gpu:
            return FilterResult.reject(
                f"node {node.node_id}: need {gpu:.1f} GiB VRAM, {node.vram_free_gib:.1f} free",
                tqerr.BLOCK_VRAM_EXCEEDS_SYSTEM,
            )
    return FilterResult.accept()


def filter_task(
    row: Any,
    node: Node,
    ctx: FilterContext,
    *,
    cpu: float,
    ram: float,
    gpu: float,
    attempt_count: int | None = None,
) -> FilterResult:
    """Hard filters shared by the local master and remote workers (P2)."""
    if _row_get(row, "requires_stable", False) and not node.stable:
        return FilterResult.reject(
            "node not stable (requires_stable=true)",
            "tq.filter.requires_stable",
        )
    # Lazy: cheap filters first, the (possibly slow) service probe last.
    for check in (
        lambda: filter_draining(node),
        lambda: filter_project_allowed(row, node),
        lambda: filter_dispatch(row, node, ctx),
        lambda: filter_requires(row, node),
        lambda: filter_trust(row, node),
        lambda: filter_freeze_pause(ctx),
        lambda: filter_services(ctx),
    ):
        step = check()
        if not step.ok:
            return step
    if attempt_count is not None:
        att = filter_max_attempts(row, attempt_count)
        if not att.ok:
            return att
    if ctx.backoff_left_sec > 0 and not ctx.force:
        return FilterResult.reject(
            f"retry backoff: {int(ctx.backoff_left_sec) + 1}s left after failed runs (tq retry starts it now)",
            tqerr.BLOCK_BACKOFF,
        )
    for step in (
        filter_exclusive(row, ctx),
        filter_global_limits(ctx, cpu=cpu, ram=ram),
        filter_project_min(ctx),
        filter_project_limits(row, ctx, cpu=cpu, ram=ram, gpu=gpu),
        filter_set_caps(ctx),
        filter_deps(row, ctx),
        filter_mutex(row, ctx),
        filter_node_capacity(node, ctx, cpu=cpu, ram=ram, gpu=gpu),
    ):
        if not step.ok:
            return step
    return FilterResult.accept()
