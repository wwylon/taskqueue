"""Scheduler: admission filters and the local admit loop.

HTTP and the worker claim path must not decide whether a task can run; they
call this module. The executor (runner / worker spawn) must not pick the next
task.

P1: extract from daemon.py with no behavior change.
P2: one Filter-Score-Reserve-Assign over a Node (local master or worker).
"""

from __future__ import annotations

import functools
import json
import logging
import sqlite3
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tqdaemon import config as tqconfig
from tqdaemon import errors as tqerr
from tqdaemon._db_runs import get_last_run_for_task, get_last_runs_for_tasks
from tqdaemon._db_tasks import (
    count_online_workers,
    failed_attempt_counts_bulk,
    get_task,
    set_task_error,
    set_task_status,
)
from tqdaemon.backoff import backoff_left_bulk as _backoff_left_bulk
from tqdaemon.db import (
    get_config,
)
from tqdaemon.estimate import estimate_task_resources
from tqdaemon.jsonutil import json_list
from tqdaemon.models import (
    STATUS_FAILED,
    STATUS_INTERRUPTED,
    STATUS_STOPPED,
    STATUS_SUSPENDED,
    task_key,
)
from tqdaemon.resources import (
    ResourceBudget,
    ram_used_gib,
)
from tqdaemon.rows import has_col
from tqdaemon.runner import ActiveRun

RunKey = tuple[str, str]


_log = logging.getLogger(__name__)


# Cross-tick estimate cache: avoids re-querying run history every tick for tasks
# whose resource history has not changed. Keyed by (project, task_id, default_ram_gib,
# predict_runs). Per-task eviction: only entries whose run history changed are dropped,
# not the whole cache, so a new run for task A does not evict estimates for task B.
_estimate_cross_tick: dict = {}
_estimate_gen: tuple | int = -1  # last observed (MAX(run_id), MAX(finished_at))
_estimate_lock = threading.Lock()


from tqdaemon._time import _now, _ts  # noqa: E402
from tqdaemon.filters import (  # noqa: E402,F401 - re-exported for existing callers
    MASTER_NODE_ID,
    FilterContext,
    FilterResult,
    Node,
    _merge_gpu_free_into_gpus,
    _norm_arch,
    _num,
    _parse_resources,
    _row_get,
    _version_matches,
    build_filter_context,
    build_set_checker,
    declared_resources,
    exclusive_drain_target,
    filter_deps,
    filter_dispatch,
    filter_draining,
    filter_exclusive,
    filter_freeze_pause,
    filter_global_limits,
    filter_max_attempts,
    filter_mutex,
    filter_node_capacity,
    filter_project_allowed,
    filter_project_limits,
    filter_project_min,
    filter_requires,
    filter_services,
    filter_set_caps,
    filter_task,
    filter_trust,
    node_from_system,
    node_from_worker,
    node_matches_requires,
    parse_worker_load,
    pick_reservation_top,
    reserved_slots_for_others,
    should_schedule_locally,
)
from tqdaemon.netutil import ProbeBlocked, check_probe_url, probe_get
from tqdaemon.rows import row_get


def _note_block(
    conn,
    project: str,
    task_id: str,
    code: str,
    reason: str,
    *,
    pending: list[tuple[str, str, str, str]] | None = None,
    status_row: Any | None = None,
) -> None:
    """Record a block reason. When *pending* is provided, defer the write."""
    row = status_row if status_row is not None else get_task(conn, project, task_id)
    if row is None:
        return
    prev_code = row_get(row, "last_error_code")
    prev_reason = row_get(row, "block_reason")
    if prev_code == code and (prev_reason or "") == reason:
        return
    if pending is not None:
        pending.append((project, task_id, code, reason))
        return
    set_task_error(conn, project, task_id, code, block_reason=reason)


def _flush_pending_blocks(
    conn,
    pending: list[tuple[str, str, str, str]],
) -> None:
    """Apply batched block_reason updates in one commit."""
    if not pending:
        return
    for project, task_id, code, reason in pending:
        set_task_error(conn, project, task_id, code, block_reason=reason, commit=False)
    conn.commit()


def build_task_status_map(
    conn: sqlite3.Connection,
    rows: list[Any] | None = None,
    *,
    projects: set[str] | None = None,
) -> dict[tuple[str, str], str]:
    """Build a dependency status map for the current candidate scope.

    Admission used to materialize every task in the database on every tick.  When
    candidate rows are supplied, include only those rows and their dependency
    peers; ``projects`` is retained for callers that already have a project
    scope but do not have candidate rows.
    """
    from tqdaemon.deprefs import row_dep_refs

    needed: set[tuple[str, str]] = set()
    if rows is not None:
        for row in rows:
            owner = str(row["project"])
            needed.add((owner, str(row["task_id"])))
            for col in ("depends_on", "depends_on_any"):
                needed.update((p, t) for p, t, _ in row_dep_refs(row, col, owner))
    elif projects:
        marks = ",".join("?" for _ in projects)
        for row in conn.execute(
            f"SELECT project, task_id FROM tasks WHERE project IN ({marks})",
            tuple(projects),
        ):
            needed.add((str(row["project"]), str(row["task_id"])))
    else:
        return {
            (str(r["project"]), str(r["task_id"])): str(r["status"])
            for r in conn.execute("SELECT project, task_id, status FROM tasks").fetchall()
        }
    if not needed:
        return {}
    from tqdaemon.queries import _fetch_status_map

    return _fetch_status_map(conn, needed)


def _is_probe_timeout(exc: BaseException) -> bool:
    """True when a health/VRAM HTTP probe hit its socket timeout."""
    if isinstance(exc, TimeoutError):
        return True
    try:
        import socket

        if isinstance(exc, socket.timeout):
            return True
    except ImportError:
        pass
    reason = getattr(exc, "reason", None)
    if reason is not None and reason is not exc and _is_probe_timeout(reason):
        return True
    msg = str(exc).lower()
    return "timed out" in msg or "timeout" in msg


def _smi_max_free_vram_gib(system_info: dict[str, Any] | None) -> float | None:
    """Best device free VRAM from a fresh nvidia-smi snapshot, or None if stale/missing."""
    if not system_info or system_info.get("gpu_probe_stale"):
        return None
    best = -1.0
    for g in system_info.get("gpus") or []:
        total = float(g.get("vram_gib") or 0.0)
        used = float(g.get("vram_used_gib") or 0.0)
        best = max(best, max(0.0, total - used))
    return best if best >= 0.0 else None


def _live_vram_headroom_ok(system_info: dict[str, Any] | None, min_vram: float) -> bool:
    """True when a non-stale nvidia-smi snapshot already has a device with enough free VRAM."""
    smi_free = _smi_max_free_vram_gib(system_info)
    if smi_free is not None:
        return smi_free + 1e-9 >= min_vram
    return False


def _ollama_name_matches_prefix(name: str, model: str, prefix: str) -> bool:
    """Prefix match on Ollama /api/ps name or model fields (e.g. llama3.2 → llama3.2:latest)."""
    p = str(prefix)
    return name.startswith(p) or model.startswith(p)


def _resident_only_ok_models(
    ps_models: list[dict[str, Any]],
    resident_ok_models: list[Any],
) -> bool:
    """True when every loaded model matches at least one allowlist prefix (and list is non-empty)."""
    if not resident_ok_models or not ps_models:
        return False
    prefixes = [str(p) for p in resident_ok_models]
    for entry in ps_models:
        name = str(entry.get("name") or "")
        model = str(entry.get("model") or "")
        if not any(_ollama_name_matches_prefix(name, model, p) for p in prefixes):
            return False
    return True


def _warm_vram_admit_gib(
    est_vram_gib: float,
    requires_services_json: str | None,
    ps_models: list[dict[str, Any]] | None,
) -> float:
    """VRAM to reserve at admit when the pinned model is already warm in Ollama."""
    if est_vram_gib <= 0 or not ps_models:
        return est_vram_gib
    try:
        services = json_list(requires_services_json)
    except (TypeError, ValueError):
        return est_vram_gib
    for svc in services or []:
        ok_list = svc.get("resident_ok_models")
        if ok_list and _resident_only_ok_models(ps_models, ok_list):
            return 0.0
    return est_vram_gib


def _ollama_ps_url_from_services(services_json: str) -> str | None:
    """Derive Ollama /api/ps URL from the first service entry with resident_ok_models."""
    import urllib.parse as _uparse

    try:
        services = json_list(services_json)
    except (TypeError, ValueError):
        return None
    for svc in services or []:
        if not svc.get("resident_ok_models"):
            continue
        url = str(svc.get("health_url") or "")
        if not url:
            continue
        try:
            _p = _uparse.urlparse(url)
            return f"{_p.scheme}://{_p.netloc}/api/ps"
        except ValueError:
            continue
    return None


_OLLAMA_PS_TTL_SEC = 3.0
_OLLAMA_PS_FAIL_TTL_SEC = 30.0  # F-205: do not re-block the admit thread on a dead endpoint
_ollama_ps_cache: dict[tuple[str, bool], tuple[float, list[dict[str, Any]] | None]] = {}
_ollama_ps_lock = __import__("threading").Lock()


def _fetch_ollama_ps_models(
    ps_url: str, *, allow_remote: bool = False, timeout: float = 1.5, background: bool = False
) -> list[dict[str, Any]] | None:
    """Fetch Ollama /api/ps models list; None on failure.

    Results (including failures) are cached so back-to-back scheduler ticks do
    not each block on a slow or dead endpoint. Failures use a longer TTL.
    F-229: fetched under the same SSRF policy as health probes.
    F-232: ``background=True`` (daemon admit) never blocks — an expired entry is
    returned stale while it refreshes; with none, None (no warm-model credit) until
    the first fetch lands.
    """
    key = (ps_url, bool(allow_remote))
    now = time.monotonic()
    with _ollama_ps_lock:
        hit = _ollama_ps_cache.get(key)
        if hit is not None:
            ttl = _OLLAMA_PS_FAIL_TTL_SEC if hit[1] is None else _OLLAMA_PS_TTL_SEC
            if now - hit[0] < ttl:
                return None if hit[1] is None else list(hit[1])
    if background:
        with _ollama_ps_lock:
            start = key not in _ollama_ps_inflight
            _ollama_ps_inflight.add(key)
        if start:

            def _run() -> None:
                try:
                    _ollama_ps_refresh(key, ps_url, allow_remote, timeout)
                finally:
                    with _ollama_ps_lock:
                        _ollama_ps_inflight.discard(key)

            threading.Thread(target=_run, name="tq-ollama-ps", daemon=True).start()
        return None if hit is None or hit[1] is None else list(hit[1])
    models = _ollama_ps_refresh(key, ps_url, allow_remote, timeout)
    return None if models is None else list(models)


_ollama_ps_inflight: set[tuple[str, bool]] = set()


def _ollama_ps_refresh(
    key: tuple[str, bool], ps_url: str, allow_remote: bool, timeout: float
) -> list[dict[str, Any]] | None:
    from tqdaemon.netutil import probe_get

    models: list[dict[str, Any]] | None
    try:
        status, body = probe_get(ps_url, allow_remote=allow_remote, timeout=timeout)
        models = list(json.loads(body).get("models") or []) if status < 400 else None
    except Exception:  # noqa: BLE001 - any probe failure means no model list
        models = None
    with _ollama_ps_lock:
        if len(_ollama_ps_cache) > 32:
            _ollama_ps_cache.clear()
        _ollama_ps_cache[key] = (time.monotonic(), models)
    return models


def _check_services(
    services_json: str,
    *,
    allow_remote: bool = False,
    system_info: dict[str, Any] | None = None,
) -> tuple[str | None, str]:
    """R1+S1: probe each requires_services entry.

    Returns (block_reason, error_code) where block_reason is None on success.
    error_code is tq.block.service_down or tq.block.service_vram.

    A VRAM-probe timeout is a transient (service_down, retry), not
    tq.block.service_vram — that code means we *measured* insufficient free VRAM.
    """
    import urllib.parse as _uparse

    try:
        services = json_list(services_json)
    except (TypeError, ValueError):
        return None, ""
    for svc in services or []:
        url = svc.get("health_url", "")
        if not url:
            continue
        timeout = float(svc.get("timeout_sec", 5))
        name = svc.get("name", url)
        # F-012 / F-229: loopback-only unless notify_allow_remote; every redirect hop is
        # checked before it is followed (the old check ran after urllib had followed it).
        why = check_probe_url(url, allow_remote=allow_remote)
        if why:
            return f"service '{name}' health_url blocked: {why}", tqerr.BLOCK_SERVICE
        try:
            status, _body = probe_get(url, allow_remote=allow_remote, timeout=timeout)
        except ProbeBlocked as exc:
            return f"service '{name}' {exc.reason}", tqerr.BLOCK_SERVICE
        except Exception as exc:  # noqa: BLE001 - any probe failure reports the service as unreachable
            return f"service '{name}' unreachable: {exc}", tqerr.BLOCK_SERVICE
        if status >= 400:
            return f"service '{name}' returned HTTP {status}", tqerr.BLOCK_SERVICE

        # S1: optional VRAM headroom check (catches Ollama keep_alive residue)
        min_vram = svc.get("min_vram_free_gib")
        if min_vram is None:
            continue
        # Live nvidia-smi already includes Ollama weights. Skip a second HTTP
        # probe when a device clearly has enough free VRAM.
        if _live_vram_headroom_ok(system_info, float(min_vram)):
            continue
        vram_probe_url = svc.get("vram_probe_url")
        if not vram_probe_url:
            # Default: derive /api/ps from health_url host (works for Ollama)
            try:
                _p = _uparse.urlparse(url)
                vram_probe_url = f"{_p.scheme}://{_p.netloc}/api/ps"
            except ValueError:
                vram_probe_url = None
        if not vram_probe_url:
            continue
        try:
            # F-229: a custom vram_probe_url gets the same SSRF policy as health_url.
            _st2, _body2 = probe_get(vram_probe_url, allow_remote=allow_remote, timeout=timeout)
            if _st2 >= 400:
                return f"service '{name}' VRAM probe returned HTTP {_st2}", tqerr.BLOCK_SERVICE
            ps = json.loads(_body2)
            ps_models = list(ps.get("models") or [])
            resident_ok = svc.get("resident_ok_models")
            if resident_ok and _resident_only_ok_models(ps_models, resident_ok):
                continue
            vram_used_bytes = sum(m.get("size_vram", 0) for m in ps_models)
            ollama_resident_gib = vram_used_bytes / (1024**3)
            vram_total_gib = float(svc.get("vram_total_gib", 0.0) or 0.0)
            if not vram_total_gib and system_info:
                vram_total_gib = float(system_info.get("gpu_vram_total_gib") or 0.0)
            smi_free = _smi_max_free_vram_gib(system_info)
            if smi_free is not None:
                # Prefer live nvidia-smi for gate + message — Ollama accounting
                # (total − resident weights) ignores other CUDA users and does not
                # move when unrelated VRAM is released.
                vram_free_gib = smi_free
                in_use_gib = max(0.0, vram_total_gib - smi_free) if vram_total_gib else 0.0
                in_use_label = "in use"
            elif vram_total_gib:
                vram_free_gib = vram_total_gib - ollama_resident_gib
                in_use_gib = ollama_resident_gib
                in_use_label = "resident"
            else:
                # No total known — block if any model is resident
                vram_free_gib = 0.0 if vram_used_bytes else float("inf")
                in_use_gib = ollama_resident_gib
                in_use_label = "resident"
            if vram_free_gib < float(min_vram):
                return (
                    f"service '{name}': VRAM not ready — "
                    f"{vram_free_gib:.1f} GiB free < {min_vram} GiB required "
                    f"({in_use_label}: {in_use_gib:.1f} GiB)",
                    tqerr.BLOCK_SERVICE_VRAM,
                )
        except ProbeBlocked as exc:
            return f"service '{name}' VRAM probe blocked: {exc.reason}", tqerr.BLOCK_SERVICE
        except Exception as exc:  # noqa: BLE001 - any probe failure reports the service as unreachable
            if _is_probe_timeout(exc):
                return (
                    f"service '{name}' VRAM probe timed out (retrying)",
                    tqerr.BLOCK_SERVICE,
                )
            return f"service '{name}' VRAM probe failed: {exc}", tqerr.BLOCK_SERVICE

    return None, ""


# F-078: both the daemon admit loop and worker claim call this. A per-tick
# dict is not enough — the next tick (and every worker poll) would probe again.
_SVC_PROBE_TTL_SEC = 30.0
_SVC_PROBE_CACHE_MAX = 256
# H4: OrderedDict for proper LRU eviction (move-to-end on hit, pop-from-front on evict).
_svc_probe_cache: OrderedDict[tuple, tuple[float, tuple[str | None, str]]] = OrderedDict()
_svc_probe_lock = threading.Lock()


def clear_service_probe_cache() -> None:
    """Test helper: drop the process-local service-probe cache."""
    with _svc_probe_lock:
        _svc_probe_cache.clear()
        _svc_inflight.clear()


def _svc_cache_key(services_json: str, allow_remote: bool, system_info: dict[str, Any] | None) -> tuple:
    # ds2 §7.1: include a hash of per-GPU free VRAM so that a task unblocked after
    # VRAM frees is not served a stale "blocked" result from a cache hit.
    # F-098: include the node hostname so a worker-claim call (system_info=None or a
    # different node) never collides with a master-call result in the shared cache.
    _node_id = (system_info or {}).get("hostname", "") if system_info is not None else "\x00worker\x00"
    # Only services with a VRAM-headroom requirement depend on GPU state; keying the rest on
    # per-GPU free VRAM (which drifts every tick) made the cache miss on every probe.
    _gpu_key = 0
    if "min_vram_free_gib" in (services_json or ""):
        _gpu_key = hash(
            frozenset(
                (g.get("index", i), round(float(g.get("vram_gib", 0.0)) - float(g.get("vram_used_gib", 0.0)), 1))
                for i, g in enumerate((system_info or {}).get("gpus", []))
            )
        )
    return (services_json or "[]", bool(allow_remote), _node_id, _gpu_key)


def _svc_cache_put(key: tuple, result: tuple[str | None, str]) -> None:
    now = time.monotonic()
    with _svc_probe_lock:
        # Evict expired entries first; if still over cap, evict LRU (front).
        if key not in _svc_probe_cache and len(_svc_probe_cache) >= _SVC_PROBE_CACHE_MAX:
            expired = [k for k, (ts, _) in _svc_probe_cache.items() if (now - ts) >= _SVC_PROBE_TTL_SEC]
            for k in expired:
                del _svc_probe_cache[k]
            while len(_svc_probe_cache) >= _SVC_PROBE_CACHE_MAX:
                _svc_probe_cache.popitem(last=False)  # pop LRU (front)
        _svc_probe_cache[key] = (now, result)
        _svc_probe_cache.move_to_end(key)


# F-232: background probes for the daemon admit path (one in flight per cache key).
_svc_inflight: set[tuple] = set()
SERVICE_PROBE_PENDING = "service probe pending (first health check running)"


def _probe_in_background(
    key: tuple, services_json: str, allow_remote: bool, system_info: dict[str, Any] | None
) -> None:
    with _svc_probe_lock:
        if key in _svc_inflight:
            return
        _svc_inflight.add(key)

    def _run() -> None:
        try:
            result = _check_services(services_json, allow_remote=allow_remote, system_info=system_info)
        except Exception as exc:  # noqa: BLE001 - a probe crash blocks the task, not the tick
            result = (f"service probe failed: {exc}", tqerr.BLOCK_SERVICE)
        _svc_cache_put(key, result)
        with _svc_probe_lock:
            _svc_inflight.discard(key)
        from tqdaemon.sched_wake import wake_scheduler

        wake_scheduler()  # re-run admission now instead of at the next tick

    threading.Thread(target=_run, name="tq-svc-probe", daemon=True).start()


@functools.lru_cache(maxsize=1024)
def _has_probes(services_json: str | None) -> bool:
    """True when any requires_services entry has a health_url (only those are probed)."""
    if not services_json or "health_url" not in services_json:
        return False
    try:
        return any(isinstance(s, dict) and s.get("health_url") for s in json_list(services_json) or [])
    except (TypeError, ValueError):
        return False


def check_services_cached(
    services_json: str,
    *,
    allow_remote: bool = False,
    system_info: dict[str, Any] | None = None,
    background: bool = False,
) -> tuple[str | None, str]:
    """``_check_services`` with a ~30s TTL. GPU VRAM state is part of the cache key.

    ``background=True`` (daemon admit, F-232): never probe on the caller's thread. A
    fresh entry is returned as usual; an expired one is returned stale while a refresh
    runs in the background; with no entry the task waits on ``SERVICE_PROBE_PENDING``
    until the first probe lands (which wakes the scheduler).
    """
    if not _has_probes(services_json):
        # Nothing to probe (the column defaults to "[]"): healthy, without a cache slot
        # or — in background mode — a spurious "pending" wait.
        return None, ""
    key = _svc_cache_key(services_json, allow_remote, system_info)
    now = time.monotonic()
    with _svc_probe_lock:
        hit = _svc_probe_cache.get(key)
        if hit is not None:
            # H4: move to end so recently-used entries survive eviction longest.
            _svc_probe_cache.move_to_end(key)
            if (now - hit[0]) < _SVC_PROBE_TTL_SEC:
                return hit[1]
    if background:
        _probe_in_background(key, services_json, allow_remote, system_info)
        return hit[1] if hit is not None else (SERVICE_PROBE_PENDING, tqerr.BLOCK_SERVICE)
    result = _check_services(services_json, allow_remote=allow_remote, system_info=system_info)
    _svc_cache_put(key, result)
    return result


# Placement helpers live in tqdaemon.placement (PC-4); re-export for callers.


def occupancy_from_running_rows(
    running_rows: list[Any],
) -> dict[str, Any]:
    """Sum declared resources across currently-running tasks."""
    n_running_by_proj: dict[str, int] = {}
    cpu_by_proj: dict[str, float] = {}
    ram_by_proj: dict[str, float] = {}
    gpu_by_proj: dict[str, float] = {}
    total_cpu = 0.0
    total_ram = 0.0
    gpu_running_by_proj: dict[str, int] = {}
    has_exclusive = False
    exclusive_task = ""
    exclusive_projects: dict[str, str] = {}
    running_locks: set[str] = set()
    for r in running_rows:
        proj = str(_row_get(r, "project", ""))
        if r is not None and _row_get(r, "exclusive", False):
            has_exclusive = True
            if not exclusive_task:
                exclusive_task = f"{proj}/{_row_get(r, 'task_id', '')}"
        if r is not None and _row_get(r, "exclusive_project", False) and proj:
            if proj not in exclusive_projects:
                exclusive_projects[proj] = f"{proj}/{_row_get(r, 'task_id', '')}"
        res0 = _parse_resources(_row_get(r, "resources", "{}"))
        cpu0 = float(res0.get("cpu_cores", 1))
        ram0 = float(res0.get("ram_gib", 0.0))
        gpu0 = float(res0.get("gpu_vram_gib", 0.0))
        for lk in res0.get("locks") or []:
            running_locks.add(str(lk))
        n_running_by_proj[proj] = n_running_by_proj.get(proj, 0) + 1
        cpu_by_proj[proj] = cpu_by_proj.get(proj, 0.0) + cpu0
        ram_by_proj[proj] = ram_by_proj.get(proj, 0.0) + ram0
        gpu_by_proj[proj] = gpu_by_proj.get(proj, 0.0) + gpu0
        total_cpu += cpu0
        total_ram += ram0
        if gpu0 > 0:
            gpu_running_by_proj[proj] = gpu_running_by_proj.get(proj, 0) + 1
    return {
        "n_running": len(running_rows),
        "has_exclusive": has_exclusive,
        "exclusive_task": exclusive_task,
        "exclusive_projects": exclusive_projects,
        "n_running_by_proj": n_running_by_proj,
        "cpu_by_proj": cpu_by_proj,
        "ram_by_proj": ram_by_proj,
        "gpu_by_proj": gpu_by_proj,
        "total_cpu_declared": total_cpu,
        "total_ram_declared": total_ram,
        "gpu_running_by_proj": gpu_running_by_proj,
        "running_locks": running_locks,
    }


def worker_slot_reserved(conn: sqlite3.Connection, worker_id: str) -> tuple[float, float, float]:
    """Declared CPU/RAM/VRAM of runs currently assigned to this worker."""
    rows = conn.execute(
        "SELECT t.resources FROM tasks t "
        "JOIN runs r ON r.project = t.project AND r.task_id = t.task_id "
        "AND r.status = 'running' AND r.worker_id = ? "
        "WHERE t.status = 'running'",
        (worker_id,),
    ).fetchall()
    cpu = ram = vram = 0.0
    for r in rows:
        c, m, g = declared_resources(r)
        cpu += c
        ram += m
        vram += g
    return cpu, ram, vram


def worker_slot_reserved_many(
    conn: sqlite3.Connection, worker_ids: list[str] | tuple[str, ...]
) -> dict[str, tuple[float, float, float]]:
    """Batch declared resource reservations for a set of workers."""
    ids = [str(worker_id) for worker_id in worker_ids]
    out = {worker_id: (0.0, 0.0, 0.0) for worker_id in ids}
    if not ids:
        return out
    marks = ",".join("?" for _ in ids)
    rows = conn.execute(
        "SELECT r.worker_id, t.resources FROM tasks t "
        "JOIN runs r ON r.project = t.project AND r.task_id = t.task_id "
        "AND r.status = 'running' "
        f"WHERE t.status = 'running' AND r.worker_id IN ({marks})",
        ids,
    ).fetchall()
    totals: dict[str, list[float]] = {worker_id: [0.0, 0.0, 0.0] for worker_id in ids}
    for row in rows:
        worker_id = str(row["worker_id"])
        if worker_id not in totals:
            continue
        cpu, ram, vram = declared_resources(row)
        totals[worker_id][0] += cpu
        totals[worker_id][1] += ram
        totals[worker_id][2] += vram
    return {worker_id: tuple(values) for worker_id, values in totals.items()}


def dep_status_from_conn(conn: sqlite3.Connection, project: str, task_id: str) -> str | None:
    row = get_task(conn, project, task_id)
    if row is None:
        return None
    return str(row["status"])


def _most_recent_first(conn: sqlite3.Connection, rows: list[Any]) -> list[Any]:
    """Order running task rows by their latest run_id, newest first.

    ``tasks`` rows have no ``run_id`` column (run ids live in ``runs``); the old inline
    ``r["run_id"]`` sort raised ``IndexError: No item with that key`` on every admit tick whenever
    more tasks ran than max_running_tasks / a project's max_tasks allowed, so the limit was never
    enforced and the scheduler tick failed (2026-10-02 log: 118 tracebacks).
    """
    if not rows:
        return []
    latest = {
        (r["project"], r["task_id"]): int(r["rid"] or 0)
        for r in conn.execute(
            "SELECT project, task_id, MAX(run_id) AS rid FROM runs WHERE status = 'running' GROUP BY project, task_id"
        ).fetchall()
    }
    return sorted(rows, key=lambda r: latest.get((r["project"], r["task_id"]), 0), reverse=True)


def p6_hold_credit(
    key: tuple[str, str], hold_key: tuple[str, str] | None, hold_ram: float, hold_vram: float
) -> tuple[float, float]:
    """(ram, vram) of the P6 hold credited back to *key*: only the exact held task gets it.

    Every other task, same project or not, sees the held headroom as unavailable.
    """
    if hold_key and key == hold_key:
        return hold_ram, hold_vram
    return 0.0, 0.0


def _shared_estimate_cache(conn: sqlite3.Connection) -> dict:
    """Cross-tick estimate cache, evicting only tasks whose run history changed.

    The generation is (MAX(run_id), MAX(finished_at)): a run that *finishes* changes the
    peaks used for prediction without adding a run_id, so new run ids alone are not enough.
    Sibling/group-based estimates borrow other tasks' history, so any change drops them too.
    """
    global _estimate_cross_tick, _estimate_gen
    _gen_row = conn.execute(
        "SELECT COALESCE(MAX(run_id), -1) AS g, COALESCE(MAX(finished_at), '') AS f FROM runs"
    ).fetchone()
    _cur_gen = (int(_gen_row["g"]), str(_gen_row["f"])) if _gen_row else (-1, "")
    with _estimate_lock:
        if _cur_gen != _estimate_gen:
            if isinstance(_estimate_gen, tuple) and _estimate_gen[0] >= 0:
                changed = conn.execute(
                    "SELECT DISTINCT project, task_id FROM runs WHERE run_id > ? OR finished_at > ?",
                    _estimate_gen,
                ).fetchall()
                _changed_keys = {(str(_cr["project"]), str(_cr["task_id"])) for _cr in changed}
                _estimate_cross_tick = {
                    k: v
                    for k, v in _estimate_cross_tick.items()
                    if k[:2] not in _changed_keys and v.get("source") not in ("sibling", "group")
                }
            else:
                _estimate_cross_tick = {}
            _estimate_gen = _cur_gen
        # F-250: hard cap so long-uptime daemons do not retain every historical key. Applied
        # on every call (not only on generation changes): keys accumulate between changes too.
        _ESTIMATE_CACHE_MAX = 4096
        if len(_estimate_cross_tick) > _ESTIMATE_CACHE_MAX:
            # Drop the oldest entries (dict insertion order).
            overflow = len(_estimate_cross_tick) - _ESTIMATE_CACHE_MAX
            for drop_key in list(_estimate_cross_tick.keys())[:overflow]:
                _estimate_cross_tick.pop(drop_key, None)
        return _estimate_cross_tick


def _p6_reserve_top(
    conn: sqlite3.Connection,
    queued: list,
    _dep_lookup: Any,
    _res_max_ticks: int,
    reservation_state: dict,
    budget: Any,
    active_runs: dict,
    _est_cache: dict,
    _vram_ps_models: Any,
    _default_ram: float,
    _predict_runs: int,
) -> tuple:
    """P6 EASY backfill: pre-reserve headroom for the top blocked task.

    Returns ``(top_row, hold_key, hold_ram_gib, hold_vram_gib)``; mutates
    ``budget`` and ``reservation_state``.
    """
    _p6_hold_key: tuple | None = None
    _p6_hold_ram = 0.0
    _p6_hold_vram = 0.0
    _top = pick_reservation_top(conn, queued, _dep_lookup) if (_res_max_ticks > 0 and queued) else None
    if _res_max_ticks > 0 and queued and _top is None:
        reservation_state.clear()
    if _top is not None:
        _top_key = (_top["project"], _top["task_id"])
        _top_res = _parse_resources(_top["resources"])
        _top_est = estimate_task_resources(
            conn,
            _top["project"],
            _top["task_id"],
            _top_res,
            default_ram_gib=_default_ram,
            predict_runs=_predict_runs,
            cache=_est_cache,
        )
        _top_gpu_admit = _warm_vram_admit_gib(
            _top_est["vram_gib"],
            row_get(_top, "requires_services"),
            _vram_ps_models,
        )
        _top_cpu = float(_top_res.get("cpu_cores", 1))
        _top_fits, _ = budget.fits(_top_cpu, _top_gpu_admit, _top_est["ram_gib"])
        # Do not P6-hold for a top task anti-flap would refuse: that phantom
        # RAM can never be claimed until live RSS drops, and it inflates
        # soft-gate messages past physical total (live + hold > installed).
        _top_last = get_last_run_for_task(conn, *_top_key)
        _top_peak = float(_top_last["peak_ram_gib"] or 0.0) if _top_last else 0.0
        _top_flap, _ = budget.antiflap_ram(_top_est["ram_gib"], _top_peak)
        if _top_flap:
            if reservation_state.get("key") == _top_key:
                reservation_state.clear()
        elif not _top_fits:
            # Top task can't start yet. Update or start the reservation counter.
            _new_p6_res = False
            if reservation_state.get("key") == _top_key:
                reservation_state["ticks"] = reservation_state.get("ticks", 0) + 1
            else:
                reservation_state.clear()
                reservation_state["key"] = _top_key
                reservation_state["ticks"] = 1
                _new_p6_res = True
            held = reservation_state["ticks"]
            if held <= _res_max_ticks:
                # Pre-hold only headroom not already in host RSS (e.g. a stopped
                # task whose process tree has not released RAM yet — live already
                # includes that footprint; reserving full est again double-counts
                # in block_reason as live + hold + est).
                _p6_overlap = 0.0
                if _top_key in active_runs:
                    _p6_overlap = float(active_runs[_top_key].current_ram_gib or 0.0)
                elif _top_last and _top_last["status"] == STATUS_STOPPED:
                    _peak = float(_top_last["peak_ram_gib"] or 0.0)
                    if _peak > 0 and budget.ram_live >= _peak * 0.8:
                        _p6_overlap = min(
                            float(_top_est["ram_gib"]),
                            _peak,
                        )
                _p6_hold_ram = max(0.0, float(_top_est["ram_gib"]) - _p6_overlap)
                _p6_overlap_vram = 0.0
                if _top_key in active_runs:
                    _p6_overlap_vram = float(active_runs[_top_key].current_vram_gib or 0.0)
                elif _top_last and _top_last["status"] == STATUS_STOPPED:
                    _peak_vram = float(_top_last["peak_vram_gib"] or 0.0)
                    if _peak_vram > 0:
                        _live_vram = sum(float(g.get("vram_used_gib", 0.0)) for g in budget.gpus)
                        if _live_vram >= _peak_vram * 0.8:
                            _p6_overlap_vram = min(
                                float(_top_gpu_admit),
                                _peak_vram,
                            )
                _p6_hold_vram = max(0.0, float(_top_gpu_admit) - _p6_overlap_vram)
                if _p6_hold_ram > 0.0 or _p6_hold_vram > 0.0:
                    budget.reserve(0, _p6_hold_vram, _p6_hold_ram)
                _p6_hold_key = _top_key
                if _new_p6_res:
                    print(
                        f"[queue] {_ts()} reservation: holding {_p6_hold_ram:.1f} GiB RAM "
                        f"+ {_p6_hold_vram:.1f} GiB VRAM for top task "
                        f"{task_key(*_top_key)} (est src={_top_est['source']}"
                        f"{f', {_p6_overlap:.1f} GiB already in live RSS' if _p6_overlap > 0 else ''})",
                        flush=True,
                    )
            else:
                # Expired: let the queue run normally for one tick, then re-evaluate.
                print(
                    f"[queue] {_ts()} reservation expired for {task_key(*_top_key)} "
                    f"after {held} ticks — releasing for one tick",
                    flush=True,
                )
                reservation_state.clear()
        else:
            # Top task can run (or is no longer queued) — clear any reservation.
            if reservation_state.get("key") == _top_key:
                reservation_state.clear()
    return _top, _p6_hold_key, _p6_hold_ram, _p6_hold_vram


def _stop_excess(conn, home: Path, candidates: list, excess: int, describe, warn_tag: str) -> int:
    """Ask up to ``excess`` of ``candidates`` (newest first) to stop; returns how many.

    ``describe(project, task_id, remaining)`` is the info line logged per stop.
    """
    from tqdaemon.db import request_stop_with_control

    stopped = 0
    for cand in candidates:
        if stopped >= excess:
            break
        proj, tid = cand["project"], cand["task_id"]
        # F-224: a victim already asked to stop counts toward the excess; re-asking it
        # every tick while it drains only rewrote its control file and logged again.
        if _row_get(cand, "stop_requested", 0):
            stopped += 1
            continue
        try:
            request_stop_with_control(conn, home, proj, tid)
            print(f"[queue] {_ts()} info: {describe(proj, tid, excess - stopped)}", file=sys.stderr)
            stopped += 1
        except Exception as exc:  # noqa: BLE001 - one stop failure must not stop the others
            print(f"[queue] {_ts()} warn: {warn_tag} {proj}/{tid}: {exc}", file=sys.stderr)
    return stopped


def _enforce_excess_running(
    conn: sqlite3.Connection,
    home: Path,
    active_runs: dict,
    running_tasks_snapshot: list,
    max_tasks: int,
    n_running: int,
    n_running_by_proj: dict,
    *,
    set_paths: dict[str, str | None] | None = None,
) -> None:
    """Stop the most recently started local tasks above the global / per-project caps."""
    from tqdaemon.project_limits import get_all_set_limits, get_effective_project_limits

    # Only local tasks are stopped here (remote workers manage themselves).
    local = [r for r in running_tasks_snapshot if (r["project"], r["task_id"]) in active_runs]
    if max_tasks > 0 and n_running > max_tasks:
        _stop_excess(
            conn,
            home,
            _most_recent_first(conn, local),
            n_running - max_tasks,
            lambda p, t, left: f"max_running_tasks={max_tasks} enforced: stopping {p}/{t} (excess={left})",
            "excess-stop",
        )

    paths = set_paths or {}
    all_sets = get_all_set_limits(conn)
    seen: set[str] = set()
    running_after = dict(n_running_by_proj)  # minus stops already requested below
    for row in running_tasks_snapshot:
        pn = row["project"]
        if pn in seen:
            continue  # one pass per project, not per running row
        seen.add(pn)
        pm = get_effective_project_limits(conn, pn, paths.get(pn), all_set_limits=all_sets).get("max_tasks", 0)
        count = n_running_by_proj.get(pn, 0)
        if pm <= 0 or count <= pm:
            continue
        running_after[pn] = count - _stop_excess(
            conn,
            home,
            _most_recent_first(conn, [r for r in local if r["project"] == pn]),
            count - pm,
            lambda p, t, left, pm=pm: f"project max_tasks={pm} enforced: stopping {p}/{t}",
            "proj-excess-stop",
        )

    # Set subtree totals: stop the newest local runs of an over-cap set (e.g. after lowering
    # bots max_tasks from 6 to 4 while 6 bot tasks run).
    from tqdaemon.set_caps import SetCapChecker

    checker = SetCapChecker(all_sets, paths)
    if checker:
        for set_path, excess in checker.over_cap(running_after):
            members = set(checker.members(set_path))
            _stop_excess(
                conn,
                home,
                _most_recent_first(conn, [r for r in local if r["project"] in members]),
                excess,
                lambda p, t, left, sp=set_path: f"set {sp} max_tasks enforced: stopping {p}/{t}",
                "set-excess-stop",
            )


def _start_local_run(conn, home: Path, project, row, key, use_resume: bool, graceful_stop: float):
    """Start one admitted task locally. Returns the ActiveRun, or None when the claim was lost
    ("already claimed") or preflight failed (task marked failed with its block code)."""
    # F-128 admit scans omit env/args_override/metadata. ActiveRun.start needs the
    # full row (args_override for `tq retry --set`, env for child env) — re-fetch.
    full = get_task(conn, row["project"], row["task_id"])
    if full is not None:
        row = full
    run = ActiveRun(
        conn=conn,
        task_row=row,
        log_dir=tqconfig.logs_dir(home, row["project"]),
        cwd=project.root,
        graceful_stop_sec=graceful_stop,
        db_path=tqconfig.db_path(home),
    )
    run.adoptable = bool(int(get_config(conn).get("adopt_on_restart", 0) or 0))
    err = run.start(use_resume=use_resume)
    if not err:
        return run
    if "already claimed" in err:
        print(f"[queue] {_ts()} skip {task_key(*key)}: {err}", flush=True)
        return None
    set_task_status(conn, row["project"], row["task_id"], STATUS_FAILED)
    code = tqerr.BLOCK_REQUIRES_PATHS if "missing required path" in err else tqerr.RUN_PREFLIGHT
    set_task_error(conn, row["project"], row["task_id"], code, block_reason=err)
    print(f"[queue] {_ts()} !! {task_key(*key)} preflight failed: {err} [{code}]", flush=True)
    return None


def _record_admission(
    conn, run, row, key, ram: float, gpu: float, source: str, budget, *, resumed: bool, forced: bool
) -> None:
    """P9: store the estimate on the run row; D11: log why the task was admitted."""
    try:
        if run.run_id:
            conn.execute(
                "UPDATE runs SET estimated_ram_gib=?, estimated_vram_gib=? WHERE run_id=?",
                (ram, gpu, run.run_id),
            )
            conn.commit()
    except sqlite3.Error as _est_exc:
        # F-129: admission still proceeds; surface estimate persistence failures.
        print(
            f"[queue] {_ts()} warn: could not store estimates for run={run.run_id}: {_est_exc}",
            file=sys.stderr,
        )
    resume_tag = " (resuming)" if resumed else ""
    force_tag = " (forced)" if forced else ""
    print(
        f"[queue] {_ts()} >> started {task_key(*key)}{resume_tag}{force_tag} "
        f"priority={row['priority']} "
        f"est_ram={ram:.1f} est_vram={gpu:.1f} src={source} "
        f"host={budget.ram_reserved:.1f}/{budget.ram_limit:.1f} soft",
        flush=True,
    )


def _local_admit_queued(
    conn: sqlite3.Connection,
    home: Path,
    projects: dict,
    active_runs: dict,
    system_info: dict,
    cfg: dict,
    graceful_stop: float,
    shutdown: bool,
    reservation_state: dict | None = None,
    *,
    host_cpu_blocked: bool = False,
    host_cpu_reason: str = "",
    admit_frozen: bool = False,
    admit_frozen_reason: str = "",
) -> None:
    """Admit queued local tasks; batched block reasons are flushed even if the body raises."""
    pending: list[tuple[str, str, str, str]] = []
    exhausted: list[tuple[str, str]] = []
    try:
        _local_admit_queued_body(
            conn,
            home,
            projects,
            active_runs,
            system_info,
            cfg,
            graceful_stop,
            shutdown,
            reservation_state,
            host_cpu_blocked=host_cpu_blocked,
            host_cpu_reason=host_cpu_reason,
            admit_frozen=admit_frozen,
            admit_frozen_reason=admit_frozen_reason,
            _pending_blocks=pending,
            _exhausted=exhausted,
        )
    finally:
        try:
            _mark_attempts_exhausted(conn, exhausted)
        except Exception:  # noqa: BLE001 - admission bookkeeping is best-effort
            _log.debug("marking attempts exhausted failed", exc_info=True)
        try:
            _flush_pending_blocks(conn, pending)
        except Exception:  # noqa: BLE001 - admission bookkeeping is best-effort
            _log.debug("flushing pending block reasons failed", exc_info=True)


def _mark_attempts_exhausted(conn, keys: list[tuple[str, str]]) -> None:
    """F-030: queued tasks past max_attempts become failed — one transaction for the whole tick."""
    if not keys:
        return

    from tqdaemon._db_tasks import transition_task

    now = _now()
    for p, t in keys:
        transition_task(conn, p, t, to="failed", from_="queued", finished=True, now=now)
    conn.commit()


@dataclass
class _OccupancyState:
    """Mutable per-tick occupancy the admit loop updates as it starts tasks."""

    n_running: int = 0
    has_exclusive: bool = False
    exclusive_task: str = ""
    exclusive_projects: dict[str, str] = field(default_factory=dict)
    n_running_by_proj: dict[str, int] = field(default_factory=dict)
    cpu_by_proj: dict[str, float] = field(default_factory=dict)
    ram_by_proj: dict[str, float] = field(default_factory=dict)
    gpu_by_proj: dict[str, float] = field(default_factory=dict)
    total_cpu_declared: float = 0.0
    total_ram_declared: float = 0.0
    running_locks: set[str] = field(default_factory=set)
    n_unknown_running: int = 0

    @classmethod
    def from_snapshot(cls, occ: dict[str, Any]) -> _OccupancyState:
        return cls(
            n_running=int(occ["n_running"]),
            has_exclusive=bool(occ["has_exclusive"]),
            exclusive_task=str(occ.get("exclusive_task") or ""),
            exclusive_projects=dict(occ["exclusive_projects"]),
            n_running_by_proj=dict(occ["n_running_by_proj"]),
            cpu_by_proj=dict(occ["cpu_by_proj"]),
            ram_by_proj=dict(occ["ram_by_proj"]),
            gpu_by_proj=dict(occ["gpu_by_proj"]),
            total_cpu_declared=float(occ["total_cpu_declared"]),
            total_ram_declared=float(occ["total_ram_declared"]),
            running_locks=set(occ["running_locks"]),
        )

    def snapshot(self) -> dict[str, Any]:
        """The shape ``build_filter_context`` reads (live references, not copies)."""
        return {
            "n_running": self.n_running,
            "has_exclusive": self.has_exclusive,
            "exclusive_task": self.exclusive_task,
            "exclusive_projects": self.exclusive_projects,
            "n_running_by_proj": self.n_running_by_proj,
            "cpu_by_proj": self.cpu_by_proj,
            "ram_by_proj": self.ram_by_proj,
            "gpu_by_proj": self.gpu_by_proj,
            "total_cpu_declared": self.total_cpu_declared,
            "total_ram_declared": self.total_ram_declared,
        }

    def add_ram(self, project: str, delta: float) -> None:
        self.ram_by_proj[project] = self.ram_by_proj.get(project, 0.0) + delta
        self.total_ram_declared += delta

    def note_admitted(
        self,
        project: str,
        task_id: str,
        *,
        cpu: float,
        ram: float,
        gpu: float,
        locks: set[str],
        unknown: bool,
        exclusive: bool,
        exclusive_project: bool,
    ) -> None:
        self.n_running += 1
        self.running_locks.update(locks)
        self.n_running_by_proj[project] = self.n_running_by_proj.get(project, 0) + 1
        self.cpu_by_proj[project] = self.cpu_by_proj.get(project, 0.0) + cpu
        self.gpu_by_proj[project] = self.gpu_by_proj.get(project, 0.0) + gpu
        self.total_cpu_declared += cpu
        self.add_ram(project, ram)
        if unknown:
            self.n_unknown_running += 1
        if exclusive:
            self.has_exclusive = True
        elif exclusive_project:
            self.exclusive_projects[project] = f"{project}/{task_id}"


def _p6_annotate_hold(
    fit_reason: str,
    hold_key: tuple[str, str],
    hold_ram: float,
    hold_vram: float,
) -> str:
    """Name the task a P6 hold was reserved for inside a fit() refusal reason."""
    for amount in (hold_ram, hold_vram):
        needle = f"hold {amount:.1f}"
        if amount > 0.05 and needle in fit_reason:
            fit_reason = fit_reason.replace(needle, f"{needle} (reserved for {task_key(*hold_key)})")
    return fit_reason


def _resource_block(
    fit_reason: str,
    ram: float,
    gpu: float,
    est: dict,
    system_vram_total: float,
) -> tuple[str, str]:
    """Block code and note for a task that does not fit the resource budget."""
    permanent = gpu > 0 and system_vram_total > 0 and gpu > system_vram_total
    code = tqerr.BLOCK_VRAM_EXCEEDS_SYSTEM if permanent else tqerr.BLOCK_RESOURCES
    est_vram = float(est["vram_gib"])
    vram_note = (
        f"admit_vram={gpu:.1f} decl_vram={est_vram:.1f}" if abs(gpu - est_vram) > 1e-9 else f"est_vram={est_vram:.1f}"
    )
    return code, (f"{fit_reason or 'insufficient resources'} [est_ram={ram:.1f} {vram_note} src={est['source']}]")


def _seed_budget_from_running(
    conn: sqlite3.Connection,
    rows: list,
    active_runs: dict,
    budget: Any,
    occ: _OccupancyState,
    settled_footprint: dict[str, tuple[float, float]],
    *,
    default_ram: float,
    predict_runs: int,
    settle_window: float,
    est_cache: dict,
) -> None:
    """Reserve the unsettled footprint of our own running tasks in ``budget``.

    Local active_runs: reserve CPU only (RAM/VRAM already in live seed).
    Remotes: exclusive awareness only — do not drain master budget.
    """
    for row in rows:
        res_r = _parse_resources(row["resources"])
        key = (row["project"], row["task_id"])
        if key not in active_runs:
            continue
        # D4: reserve the *unsettled* portion of this task's estimate — the
        # gap between what we expect it to take and what it has taken so
        # far. A CUDA job needs 30-120s to load weights, but the tick is 5s,
        # so a just-started task reads as nearly free and the scheduler
        # keeps admitting against memory that is about to disappear (F3).
        #
        # INVARIANT (do not break): budget.ram_reserved is seeded from
        # whole-host ram_used_gib(), which ALREADY includes this task's
        # current usage. Adding (est - current) tops it up to est exactly
        # once. Reserving est here instead would double-count.
        run = active_runs[key]
        est = estimate_task_resources(
            conn,
            row["project"],
            row["task_id"],
            res_r,
            default_ram_gib=default_ram,
            predict_runs=predict_runs,
            cache=est_cache,
        )
        # Candidates are charged their *estimate* against max_ram_gib; charge running
        # tasks the same way, otherwise a task counts as its estimate on the admit tick
        # and only its (often zero) declaration on the next, and the gate flaps.
        ram_delta = float(est["ram_gib"] or 0.0) - float(res_r.get("ram_gib", 0.0) or 0.0)
        if ram_delta > 0:
            occ.add_ram(row["project"], ram_delta)
        try:
            elapsed = float(run.elapsed_sec())
        except (TypeError, ValueError, AttributeError):
            elapsed = 0.0
        if est["unknown"]:
            # G5: once an unknown task passes settle_window its measured
            # footprint is trustworthy. Record it per-project so queued
            # siblings can be admitted against it rather than blocked.
            if settle_window > 0 and elapsed >= settle_window:
                measured_ram = float(run.current_ram_gib or 0.0)
                measured_vram = float(run.current_vram_gib or 0.0)
                proj_fp = settled_footprint.get(row["project"])
                if proj_fp is None or measured_ram > proj_fp[0]:
                    settled_footprint[row["project"]] = (measured_ram, measured_vram)
            else:
                occ.n_unknown_running += 1
        if settle_window > 0 and elapsed < settle_window:
            unsettled_ram = max(0.0, est["ram_gib"] - float(run.current_ram_gib or 0.0))
            unsettled_vram = max(0.0, est["vram_gib"] - float(run.current_vram_gib or 0.0))
        else:
            # Past the settle window the measurement is trustworthy, so the
            # live seed alone is correct and a stale estimate must not keep
            # withholding memory forever.
            unsettled_ram = unsettled_vram = 0.0
        budget.reserve(float(res_r.get("cpu_cores", 1)), unsettled_vram, unsettled_ram)


def _resettle_budget(budget: Any, live: float, *, admitted_ram: float) -> None:
    """Re-seed the host RAM reading after an ``admit_settle_sec`` soak (F-225).

    Overwriting ``ram_reserved`` with the live reading dropped the P6 hold, the unsettled
    reservations of other runs and the just-admitted task's estimate. Keep them, and add
    only host growth the just-admitted task (already reserved at its estimate) cannot explain.
    """
    growth = live - float(budget.ram_live)
    budget.ram_reserved += max(0.0, growth - float(admitted_ram))
    budget.ram_live = live


def _local_admit_queued_body(
    conn: sqlite3.Connection,
    home: Path,
    projects: dict,
    active_runs: dict,
    system_info: dict,
    cfg: dict,
    graceful_stop: float,
    shutdown: bool,
    reservation_state: dict | None = None,
    *,
    host_cpu_blocked: bool = False,
    host_cpu_reason: str = "",
    admit_frozen: bool = False,
    admit_frozen_reason: str = "",
    _pending_blocks: list,
    _exhausted: list | None = None,
) -> None:
    """Schedule new local tasks if resources allow (one resource budget across all projects)."""
    if _exhausted is None:
        _exhausted = []
    # Wave B3 / F-214: shared candidate fetch+fairshare (same helper as worker claim).
    _cand_window = int(cfg.get("admit_candidate_window", 256))
    # Fetch a bit more than the window so fairshare can re-order among a larger set,
    # then trim after sort. When window is 0, load all.
    _fetch_limit = None if _cand_window <= 0 else max(_cand_window * 2, _cand_window)
    from tqdaemon.admit_context import load_fairshare_candidates

    queued, _occ_raw, _usage_by_proj, _status_map, running_tasks_snapshot = load_fairshare_candidates(
        conn,
        limit=_fetch_limit,
        # F-233: ``stopped`` (pressure / preempt / evict) is auto-restartable locally; the
        # shared helper defaults to the remote-claim set (queued only).
        statuses=("queued", "stopped"),
    )
    if not queued:
        return
    if reservation_state is None:
        reservation_state = {}
    # block_reason is updated in-place by _note_block (which skips unchanged values)
    # and cleared by claim on start. No bulk NULL here — that races with UI polls.

    def _dep_lookup(p: str, t: str, _m: dict = _status_map) -> str | None:
        return _m.get((p, t))

    # Wave B5: batch block_reason writes.
    occ = _OccupancyState.from_snapshot(_occ_raw)
    from tqdaemon.project_limits import get_all_effective_project_limits, set_paths_from_projects

    _set_paths = set_paths_from_projects(projects)
    # T-205: per-project minimum slots (needs all queued rows, not just the window).
    # Effective limits include inherited set defaults.
    _all_limits = get_all_effective_project_limits(conn, _set_paths)
    _set_checker = build_set_checker(conn, _set_paths)
    _queued_by_proj: dict[str, int] = {}
    if any(float(v.get("min_tasks", 0) or 0) > 0 for v in _all_limits.values()):
        for _qr in conn.execute(
            "SELECT project, COUNT(*) AS n FROM tasks "
            "WHERE status IN ('queued','stopped') AND enabled = 1 GROUP BY project"
        ).fetchall():
            _queued_by_proj[_qr["project"]] = int(_qr["n"])
    if _cand_window > 0 and len(queued) > _cand_window:
        queued = queued[:_cand_window]
    # T-204: scavengers go last so they only see capacity normal work left idle.
    _scav_keys = {(r["project"], r["task_id"]) for r in queued if _row_get(r, "task_class", "normal") == "scavenger"}
    if _scav_keys:
        queued = sorted(queued, key=lambda r: (r["project"], r["task_id"]) in _scav_keys)
    _contended_by: list[str] = []
    budget = ResourceBudget(system_info, cfg)
    # Admission-control knobs.
    _default_ram = float(cfg.get("default_task_ram_gib", 1.0))
    _predict_runs = int(cfg.get("resource_predict_runs", 5))
    _max_unknown = int(cfg.get("max_concurrent_unknown", 1))
    _settle_window = float(cfg.get("settle_window_sec", 180))
    _max_per_tick = int(cfg.get("admit_max_per_tick", 1))
    _settle_sec = float(cfg.get("admit_settle_sec", 0.0))
    _shadow = bool(int(cfg.get("admission_shadow_mode", 0)))
    _res_max_ticks = int(cfg.get("reservation_max_ticks", 60))
    _est_cache = _shared_estimate_cache(conn)
    _svc_cache: dict[str, tuple[str | None, str]] = {}
    # F-232: probes run off the tick (config escape hatch: service_probe_background=0).
    _svc_background = bool(int(cfg.get("service_probe_background", 1)))
    _vram_ps_models: list[dict[str, Any]] | None = None
    for _qrow in queued:
        if not has_col(_qrow, "requires_services"):
            continue
        _ps_url = _ollama_ps_url_from_services(_qrow["requires_services"] or "[]")
        if _ps_url:
            _vram_ps_models = _fetch_ollama_ps_models(
                _ps_url, allow_remote=bool(cfg.get("notify_allow_remote")), background=_svc_background
            )
            break
    _admitted_this_tick = 0
    # G5: per-project measured footprint from settled unknown running tasks.
    _settled_unknown_footprint: dict[str, tuple[float, float]] = {}
    max_tasks = int(cfg.get("max_running_tasks", 0))
    g_max_cpu = float(cfg.get("max_cpu_cores", 0))
    g_max_ram = float(cfg.get("max_ram_gib", 0))
    online_workers = count_online_workers(conn)
    # W4.5 / Round 12 D: shared project caches with remote claim (admit_context).
    from tqdaemon.admit_context import AdmitProjectCaches

    _admit_caches = AdmitProjectCaches(set_paths=_set_paths)
    _paused_cache = _admit_caches.paused
    _frozen_cache = _admit_caches.frozen
    _proj_limits_cache = _admit_caches.limits
    _seed_budget_from_running(
        conn,
        running_tasks_snapshot,
        active_runs,
        budget,
        occ,
        _settled_unknown_footprint,
        default_ram=_default_ram,
        predict_runs=_predict_runs,
        settle_window=_settle_window,
        est_cache=_est_cache,
    )

    max_conc = int(cfg.get("max_concurrent_tasks", 0))

    # P6: EASY backfill — if the top-priority queued task is blocked only on
    # resources, pre-reserve its estimated footprint so lower-priority small
    # tasks cannot drain the headroom it needs.
    #
    # We check the top task against a bare fit() *before* the main loop so that
    # the reserved headroom is visible to every candidate in the loop. The
    # reservation_state dict is maintained across ticks (passed in by run_daemon)
    # so we can detect and expire a task that has been waiting too long.
    _top, _p6_hold_key, _p6_hold_ram, _p6_hold_vram = _p6_reserve_top(
        conn,
        queued,
        _dep_lookup,
        _res_max_ticks,
        reservation_state,
        budget,
        active_runs,
        _est_cache,
        _vram_ps_models,
        _default_ram,
        _predict_runs,
    )

    _enforce_excess_running(
        conn,
        home,
        active_runs,
        running_tasks_snapshot,
        max_tasks,
        occ.n_running,
        occ.n_running_by_proj,
        set_paths=_set_paths,
    )

    local_node = node_from_system(system_info)

    # `queued` rows are full task rows, so a block note never needs its own get_task round trip.
    _queued_rows = {(r["project"], r["task_id"]): r for r in queued}

    def _nb(project: str, task_id: str, code: str, reason: str, *, row: Any = None) -> None:
        if row is None:
            row = _queued_rows.get((project, task_id))
        if code == tqerr.BLOCK_RESOURCES and (project, task_id) not in _scav_keys:
            _contended_by.append(f"{project}/{task_id}")
        _note_block(
            conn,
            project,
            task_id,
            code,
            reason,
            pending=_pending_blocks,
            status_row=row,
        )

    _drain = exclusive_drain_target(
        # P6 already picked the top when reservations are on (None there means "no runnable top");
        # only scan again when reservations are disabled.
        _top
        if (_top is not None or (_res_max_ticks > 0 and queued))
        else pick_reservation_top(conn, queued, _dep_lookup),
        occ.n_running,
        occ.n_running_by_proj,
    )
    _last_runs = get_last_runs_for_tasks(conn, [(r["project"], r["task_id"]) for r in queued])
    # Wave C: one GROUP BY for max_attempts candidates instead of per-row COUNT.
    _attempt_keys = [
        (r["project"], r["task_id"]) for r in queued if has_col(r, "max_attempts") and int(r["max_attempts"] or 0) > 0
    ]
    _attempt_bases = {
        (r["project"], r["task_id"]): int(r["retry_attempt_baseline"] or 0)
        for r in queued
        if has_col(r, "retry_attempt_baseline") and has_col(r, "max_attempts") and int(r["max_attempts"] or 0) > 0
    }
    _attempt_counts = failed_attempt_counts_bulk(conn, _attempt_keys, baselines=_attempt_bases)
    # F-247: prefetch retry backoff for the whole window (was 2 SQL round-trips per row).
    _backoff_by_key = _backoff_left_bulk(conn, queued)
    for row in queued:
        if shutdown:
            break
        key: RunKey = (row["project"], row["task_id"])
        project = projects.get(row["project"])
        if project is None:
            # Task belongs to a project no longer in the registry — leave queued.
            continue
        force = bool(row["force_run"]) if has_col(row, "force_run") else False
        if _drain is not None and key != _drain[0] and not force and (_drain[1] is None or _drain[1] == row["project"]):
            _nb(
                row["project"],
                row["task_id"],
                tqerr.BLOCK_EXCLUSIVE if _drain[1] is None else tqerr.BLOCK_EXCLUSIVE_PROJECT,
                f"draining for exclusive task {task_key(*_drain[0])}",
            )
            continue
        # Host CPU soft-gate and recovery cooldown skip the whole admit loop
        # in daemon.py for ordinary tasks. force_run is the documented bypass
        # (UI Force / `tq force`) and must still reach start().
        if admit_frozen and not force:
            if admit_frozen_reason:
                _nb(
                    row["project"],
                    row["task_id"],
                    tqerr.BLOCK_RESOURCES,
                    admit_frozen_reason,
                )
            continue
        if host_cpu_blocked and not force:
            if host_cpu_reason:
                _nb(
                    row["project"],
                    row["task_id"],
                    tqerr.BLOCK_RESOURCES,
                    host_cpu_reason,
                )
            continue
        if key in _scav_keys and _contended_by and not force:
            _nb(
                row["project"],
                row["task_id"],
                tqerr.BLOCK_SCAVENGER,
                f"scavenger: capacity contended by {_contended_by[0]}",
            )
            continue
        proj_key = row["project"]
        _frozen_cache[proj_key], _paused_cache[proj_key], _proj_limits_cache[proj_key] = _admit_caches.project_state(
            conn, proj_key
        )
        # filter_freeze_pause runs before filter_services, so a frozen/paused
        # project never needs its (possibly slow) service probes.
        if has_col(row, "requires_services") and not _frozen_cache[proj_key] and not _paused_cache[proj_key]:
            _svc_raw = row["requires_services"] or "[]"
            if _svc_raw not in _svc_cache:
                _svc_cache[_svc_raw] = check_services_cached(
                    _svc_raw,
                    allow_remote=bool(cfg.get("notify_allow_remote")),
                    system_info=system_info,
                    background=_svc_background,
                )
            _svc_block, _svc_code = _svc_cache[_svc_raw]
        else:
            _svc_block, _svc_code = None, ""

        max_a = int(row["max_attempts"] or 0) if has_col(row, "max_attempts") else 0
        _attempts = _attempt_counts.get(key, 0) if max_a > 0 else 0
        if max_a > 0 and _attempts >= max_a:
            # F-030: mark failed so task doesn't stay stuck in queued forever
            _exhausted.append((row["project"], row["task_id"]))  # marked failed in one batch after the loop
            _nb(
                row["project"],
                row["task_id"],
                tqerr.BLOCK_MAX_ATTEMPTS,
                f"max_attempts={max_a} reached — marked failed",
            )
            continue

        res = _parse_resources(row["resources"])
        cpu = _num(res.get("cpu_cores"), 1.0)
        # D1: gate on the *estimate*, not the raw declaration. An undeclared
        # ram_gib/gpu_vram_gib used to be accounted as zero, which silently
        # disabled every resource gate for that task (F2).
        _est = estimate_task_resources(
            conn,
            row["project"],
            row["task_id"],
            res,
            default_ram_gib=_default_ram,
            predict_runs=_predict_runs,
            cache=_est_cache,
        )
        gpu = _warm_vram_admit_gib(
            _est["vram_gib"],
            row_get(row, "requires_services"),
            _vram_ps_models,
        )
        ram = _est["ram_gib"]
        exclusive = bool(row["exclusive"])
        exclusive_project = bool(_row_get(row, "exclusive_project", False))
        proj_name = row["project"]

        # D5: cap concurrent tasks we know nothing about (no declaration, no
        # history, no sibling). matrix: expands one YAML into N same-shaped
        # zero-history tasks; without this they all start together.
        #
        # G5: once the first unknown task from this project settles (elapsed >=
        # settle_window_sec), its measured footprint is stored in
        # _settled_unknown_footprint. Use it as the estimate so the gate opens
        # and siblings can start — we now have real data, not guesses.
        if _est["unknown"] and _max_unknown > 0 and occ.n_unknown_running >= _max_unknown:
            _fp = _settled_unknown_footprint.get(proj_name)
            if _fp is not None:
                # Override estimate with settled sibling's measured footprint.
                ram = max(ram, _fp[0])
                gpu = max(gpu, _fp[1])
                # Don't block — fall through to normal resource check below.
            else:
                _nb(
                    proj_name,
                    row["task_id"],
                    tqerr.BLOCK_RESOURCES,
                    f"max_concurrent_unknown={_max_unknown} reached — waiting for a "
                    f"task with no declared resources and no run history to settle "
                    f"(settle_window_sec={_settle_window:.0f}s) so its footprint "
                    f"can be measured",
                )
                continue

        _occ_snapshot = occ.snapshot()
        _cfg_snapshot = {
            "max_running_tasks": max_tasks,
            "max_cpu_cores": g_max_cpu,
            "max_ram_gib": g_max_ram,
        }
        ctx = build_filter_context(
            _occ_snapshot,
            _cfg_snapshot,
            frozen=_frozen_cache[proj_key],
            paused=_paused_cache[proj_key],
            project_limits=_proj_limits_cache[proj_key],
            svc_block=_svc_block,
            svc_code=_svc_code or "",
            dep_status=_dep_lookup,
            online_workers=online_workers,
            force=force,
            skip_node_capacity=True,
            running_locks=occ.running_locks,
            min_reserved_others=reserved_slots_for_others(
                _all_limits,
                _queued_by_proj,
                occ.n_running_by_proj,
                proj_key,
            ),
            backoff_left_sec=_backoff_by_key.get(key, 0.0),
            set_block=(
                _set_checker.check(
                    proj_key,
                    _occ_snapshot["n_running_by_proj"],
                    _occ_snapshot["cpu_by_proj"],
                    _occ_snapshot["ram_by_proj"],
                    _occ_snapshot["gpu_by_proj"],
                    cpu=cpu,
                    ram=ram,
                    gpu=gpu,
                )
                if _set_checker
                else None
            ),
        )
        fr = filter_task(row, local_node, ctx, cpu=cpu, ram=ram, gpu=gpu)
        if not fr.ok:
            if not fr.silent:
                _nb(row["project"], row["task_id"], fr.code or tqerr.BLOCK_RESOURCES, fr.reason)
            continue
        last_run = _last_runs.get(key)
        if not force:
            peak = float(last_run["peak_ram_gib"] or 0.0) if last_run else 0.0
            flap, flap_reason = budget.antiflap_ram(ram, peak)
            if flap:
                _nb(
                    row["project"],
                    row["task_id"],
                    tqerr.BLOCK_RESOURCES,
                    flap_reason,
                )
                continue
        # P6 hold was reserved for this specific task — credit it so block_reason
        # does not show hold + est for the same footprint.  Only the exact held
        # task receives the credit; all other tasks (same project or cross-project)
        # see the hold as unavailable headroom.
        _hold_credit_ram, _hold_credit_gpu = p6_hold_credit(key, _p6_hold_key, _p6_hold_ram, _p6_hold_vram)
        ok, fit_reason = budget.fits(
            cpu,
            gpu,
            ram,
            ram_hold_credit=_hold_credit_ram,
            gpu_hold_credit=_hold_credit_gpu,
        )
        if not ok and _p6_hold_key and key != _p6_hold_key and fit_reason:
            fit_reason = _p6_annotate_hold(fit_reason, _p6_hold_key, _p6_hold_ram, _p6_hold_vram)
        if not ok and _shadow:
            # Shadow mode: log the decision we *would* have made without acting
            # on it, so a new gate can be validated against a live queue before
            # it is allowed to block real work.
            print(
                f"[queue] {_ts()} SHADOW would-block {task_key(*key)}: {fit_reason} "
                f"[est_ram={ram:.1f} est_vram={gpu:.1f} src={_est['source']}]",
                flush=True,
            )
            ok = True
        if not ok and not force:
            block_code, block_note = _resource_block(
                fit_reason,
                ram,
                gpu,
                _est,
                float(system_info.get("gpu_vram_total_gib") or 0),
            )
            _nb(row["project"], row["task_id"], block_code, block_note)
            continue
        if not ok and force:
            print(
                f"[queue] {_ts()} WARN force_run bypassing resource gate for {task_key(*key)}: {fit_reason}",
                flush=True,
            )

        # Locks are enforced by filter_task (filter_mutex reads the live occ.running_locks set).
        task_locks = set(res.get("locks") or [])

        if max_conc > 0 and occ.n_running >= max_conc and not force:
            _nb(
                row["project"],
                row["task_id"],
                tqerr.BLOCK_RESOURCES,
                f"max_concurrent_tasks={max_conc} reached ({occ.n_running} running)",
            )
            continue

        # Resume after interrupt, cooperative suspend, or RAM stop.
        use_resume = bool(last_run and last_run["status"] in (STATUS_INTERRUPTED, STATUS_SUSPENDED, STATUS_STOPPED))
        run = _start_local_run(conn, home, project, row, key, use_resume, graceful_stop)
        if run is None:
            continue

        active_runs[key] = run
        budget.reserve(cpu, gpu, ram)
        # F-241: host GPU lock file for local pytest / sibling processes.
        try:
            from tqdaemon.gpu_lock import acquire as _gpu_lock_acquire
            from tqdaemon.resources import task_requires_gpu as _task_requires_gpu

            if _task_requires_gpu(row["resources"]) and run.proc is not None:
                _gpu_lock_acquire(
                    home,
                    project=row["project"],
                    task_id=row["task_id"],
                    run_id=int(run.run_id or 0),
                    pid=int(run.proc.pid),
                    started=float(run.started_at or 0.0) or None,
                )
        except Exception:  # noqa: BLE001 - lock file is advisory
            _log.debug("gpu.lock acquire failed", exc_info=True)
        # F-083 / §6.9: clear sticky P6 hold once the held task (or any path) admits it.
        if reservation_state.get("key") == key:
            reservation_state.clear()
        occ.note_admitted(
            proj_name,
            row["task_id"],
            cpu=cpu,
            ram=ram,
            gpu=gpu,
            locks=task_locks,
            unknown=bool(_est["unknown"]),
            exclusive=exclusive,
            exclusive_project=exclusive_project,
        )
        _record_admission(conn, run, row, key, ram, gpu, _est["source"], budget, resumed=use_resume, forced=force)
        if exclusive:
            break

        # D5: stagger starts. Admitting every eligible task in one tick lets N
        # tasks each size themselves against the same pre-start snapshot (F4).
        _admitted_this_tick += 1
        if _max_per_tick > 0 and _admitted_this_tick >= _max_per_tick:
            break
        if _settle_sec > 0:
            # Opt-in extra soak: let the just-started task allocate, then
            # re-seed the host reading before considering the next candidate.
            time.sleep(_settle_sec)
            try:
                _resettle_budget(budget, ram_used_gib(), admitted_ram=ram)
            except RuntimeError:
                break
