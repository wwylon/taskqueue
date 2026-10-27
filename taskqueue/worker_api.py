"""Atomic remote-worker task claim logic, extracted from web.py."""

from __future__ import annotations
from taskqueue._db_tasks import get_task
from taskqueue.jsonutil import json_list

import json
import sqlite3
from typing import Any
import time
from pathlib import Path
from typing import Callable
from taskqueue.rows import row_get

# F-100: GC orphan overlays every N claim attempts (cheap I/O; not every call).
_GC_ORPHAN_INTERVAL = 20
_gc_claim_counter = 0

# D2: 3s TTL cache for DISTINCT requires_services — avoids a full-table scan on
# every worker claim (20 workers × 1/s = 20k wasted row-reads/s without this).
_QUEUED_SVC_CACHE: dict[str, tuple[float, list]] = {}
_QUEUED_SVC_CACHE_TTL = 3.0

# M3: 5s TTL caches for read-pass config + occupancy — shared across concurrent
# worker claims within the same polling window. Write-pass (cfg_fresh, occ_fresh
# inside BEGIN IMMEDIATE) is always fresh and must NOT use these caches.
# Keyed by str(db_path) so different test databases (tmp_path) never share entries.
_CONFIG_CACHE: dict[str, tuple[float, dict]] = {}
_OCC_CACHE: dict[str, tuple[float, list]] = {}
_CLAIM_CACHE_TTL = 5.0


def _capture_code_package(selected_task, proj: str, projects_ref: Callable[[], dict]) -> tuple[dict | None, str | None]:
    """Capture code package before claim commit (must not run under BEGIN IMMEDIATE).

    F-084: callers prepare the package before claiming so failure never leaves a
    ``running`` row. For ``git_patch`` / ``bundle``, failures raise so the claim
    is aborted — never silently fall through to shared ``project_root``.
    """
    code_package: dict | None = None
    package_hash: str | None = None
    proj_obj = projects_ref().get(proj)
    from taskqueue.provision import (
        PROVIDER_BUNDLE,
        PROVIDER_GIT_PATCH,
        PROVIDER_PROJECT_ROOT,
        capture_git_package,
        package_from_bundle,
        resolve_code_provider,
    )
    from taskqueue.config import state_home

    bh = None
    if "bundle_hash" in selected_task.keys():
        bh = selected_task["bundle_hash"]
    cp_raw = ""
    if "code_provider" in selected_task.keys():
        cp_raw = selected_task["code_provider"] or ""
    provider = resolve_code_provider(
        raw_provider=cp_raw or None,
        bundle_hash=bh,
        project_root=proj_obj.root if proj_obj else None,
    )
    if provider == PROVIDER_PROJECT_ROOT:
        return None, None
    try:
        if provider == PROVIDER_GIT_PATCH:
            if proj_obj is None:
                raise RuntimeError(f"project '{proj}' not loaded for git_patch capture")
            pkg = capture_git_package(
                proj_obj.root, project=proj, home=state_home()
            )
            code_package = pkg.to_dict()
            package_hash = pkg.package_hash
        elif provider == PROVIDER_BUNDLE:
            if not bh:
                raise RuntimeError("bundle provider requires bundle_hash")
            env_h = ""
            if proj_obj is not None:
                from taskqueue.provision import env_lock_hash
                env_h = env_lock_hash(proj_obj.root)
            pkg = package_from_bundle(proj, str(bh), env_hash=env_h)
            code_package = pkg.to_dict()
            package_hash = pkg.package_hash
        else:
            raise RuntimeError(f"unsupported code provider {provider!r}")
    except Exception as exc:
        import sys as _sys
        print(
            f"[worker_api] code package capture failed for {proj}/{selected_task['task_id']}: {exc}",
            file=_sys.stderr,
        )
        raise
    if not code_package or not package_hash:
        raise RuntimeError(f"code package capture returned empty for provider {provider}")
    return code_package, package_hash



def _svc_key(raw: str) -> str:
    try:
        return json.dumps(json_list(raw), sort_keys=True)
    except Exception:
        return raw or "[]"


def _precheck_services(rdb: sqlite3.Connection, db_key: str, cfg: dict) -> dict[str, str | None]:
    """Probe each distinct ``requires_services`` of queued tasks (TTL-cached).

    Runs on the read connection so the 5 s health checks never sit inside
    BEGIN IMMEDIATE.  Returns ``{svc_key: block_reason | None}``.
    """
    from taskqueue.scheduler import check_services_cached

    allow_remote = bool(cfg.get("notify_allow_remote"))
    svc_cache_at, pre_queued = _QUEUED_SVC_CACHE.get(db_key, (0.0, []))
    if not pre_queued or time.monotonic() - svc_cache_at >= _QUEUED_SVC_CACHE_TTL:
        pre_queued = rdb.execute(
            "SELECT DISTINCT requires_services FROM tasks WHERE status = 'queued' AND enabled = 1"
        ).fetchall()
        _QUEUED_SVC_CACHE[db_key] = (time.monotonic(), pre_queued)
    # Probe system once for VRAM headroom so service checks can skip slow Ollama probes.
    try:
        from taskqueue.resources import detect_system as _detect_sys
        system_info: dict | None = _detect_sys()
    except Exception:
        system_info = None
    out: dict[str, str | None] = {}
    for pr in pre_queued:
        sv = (row_get(pr, "requires_services")) or "[]"
        key = _svc_key(sv)
        if key not in out:
            # F-085: use F-078 TTL cache (was raw _check_services every claim).
            out[key] = check_services_cached(
                sv, allow_remote=allow_remote, system_info=system_info
            )[0]
    return out


def _online_peer_nodes(rdb: sqlite3.Connection, cfg: dict) -> list:
    """Placement nodes for every online, healthy worker (with slot reservations)."""
    from taskqueue.db import worker_health
    from taskqueue.scheduler import node_from_worker, worker_slot_reserved

    heartbeat_sec = float(cfg.get("heartbeat_sec", 30))
    nodes = []
    for wrow in rdb.execute("SELECT * FROM workers WHERE status = 'online'").fetchall():
        if worker_health(wrow["last_seen"], heartbeat_sec=heartbeat_sec) != "online":
            continue
        rc, rr, rv = worker_slot_reserved(rdb, wrow["worker_id"])
        nodes.append(node_from_worker(wrow, reserved_cpu=rc, reserved_ram=rr, reserved_vram=rv))
    return nodes


def _claim_response(
    claimed_row_dict: dict, projects_ref: Callable[[], dict], proj: str, run_id: int,
    log_p, use_resume: bool, lease_ttl: int, finalize_token: str,
) -> dict:
    """Build the JSON response for a successful claim (task payload + run handles)."""
    from taskqueue.config import PROTOCOL_VERSION
    from taskqueue.web import _task_dict, relativize_requires_paths

    task_payload = _task_dict(claimed_row_dict)
    proj_obj = projects_ref().get(proj)
    if proj_obj is not None and isinstance(task_payload.get("requires_paths"), list):
        task_payload["requires_paths"] = relativize_requires_paths(
            task_payload["requires_paths"], proj_obj.root
        )
    try:
        task_payload["args"] = json_list(claimed_row_dict.get("args"))
    except Exception:
        pass
    task_payload.pop("args_override", None)
    return {
        "task": task_payload,
        "run_id": run_id,
        "log_path": str(log_p),
        "use_resume": use_resume,
        "lease_ttl_sec": lease_ttl,
        "protocol": PROTOCOL_VERSION,
        "finalize_token": finalize_token,  # F-055: must be echoed back on finalize
    }


def _maybe_gc_orphan_packages(db_path: Path) -> None:
    """F-100: every _GC_ORPHAN_INTERVAL claims, GC overlay files left by failed/raced claims."""
    global _gc_claim_counter
    _gc_claim_counter += 1
    if _gc_claim_counter % _GC_ORPHAN_INTERVAL == 0:
        try:
            from taskqueue.provision import gc_orphan_packages
            from taskqueue.config import state_home
            gc_orphan_packages(state_home(), db_path)
        except Exception:
            pass


def _insert_claimed_run(
    wdb: sqlite3.Connection, db_path: Path, still_queued: Any, worker_fresh: Any, worker_id: str,
    selected_proj: str, selected_tid: str, package_hash: str | None,
    selected_ram: float, selected_gpu: float,
) -> tuple[int, str, Path, dict, int] | None:
    """Write pass inside the caller's BEGIN IMMEDIATE: insert the run, flip the task to running,
    bake a one-shot args_override (F-063). Returns (run_id, finalize_token, log_path, task dict, lease ttl),
    or None when the task was claimed by someone else (caller rolls back)."""
    from taskqueue._time import _now
    from taskqueue.config import LEASE_TTL_SEC, logs_dir
    from taskqueue.db import lease_expiry_iso

    started = _now()
    _log_ts = started.replace(":", "").replace("-", "").replace("T", "_").split("+")[0].split(".")[0]
    log_p = logs_dir(db_path.parent, selected_proj) / f"{selected_tid}_remote_{worker_id}_{_log_ts}.log"
    log_p.parent.mkdir(parents=True, exist_ok=True)

    _p50 = row_get(still_queued, "predicted_duration_sec")
    _lease_ttl = int(max(30, min(300, 4 * float(_p50)))) if _p50 else LEASE_TTL_SEC
    lease_exp = lease_expiry_iso(ttl_sec=_lease_ttl)
    import secrets as _secrets
    finalize_token = _secrets.token_hex(16)  # F-055: per-claim secret
    worker_session = None
    if "session_id" in worker_fresh.keys():
        worker_session = worker_fresh["session_id"]

    cur = wdb.execute(
        "INSERT INTO runs (project, task_id, started_at, status, log_path, worker_id, "
        "lease_expires_at, finalize_token, worker_session_id, package_hash, "
        "estimated_ram_gib, estimated_vram_gib) "
        "VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            selected_proj, selected_tid, started, str(log_p), worker_id, lease_exp,
            finalize_token, worker_session, package_hash,
            float(selected_ram), float(selected_gpu),
        )
    )
    run_id = cur.lastrowid

    cur_claim = wdb.execute(
        "UPDATE tasks SET status = 'running', force_run = 0, "
        "block_reason = NULL, last_error_code = NULL, last_run_id = ?, updated_at = ? "
        "WHERE project = ? AND task_id = ? AND status = 'queued' AND enabled = 1",
        (run_id, started, selected_proj, selected_tid),
    )
    if (cur_claim.rowcount or 0) == 0:
        return None

    # F-063 / F-005: bake one-shot override in the SAME transaction as the claim.
    raw_override = None
    if "args_override" in still_queued.keys():
        raw_override = still_queued["args_override"]
    if raw_override:
        try:
            json.loads(raw_override)  # validate JSON
            wdb.execute(
                "UPDATE tasks SET args = ?, args_override = NULL "
                "WHERE project = ? AND task_id = ?",
                (raw_override, selected_proj, selected_tid),
            )
        except ValueError:
            # Malformed one-shot override: drop it so it cannot wedge later claims.
            # The run proceeds with the task's own args (logged, not silent).
            print(f"[worker_api] invalid args_override for {selected_proj}/{selected_tid}; cleared", flush=True)
            wdb.execute(
                "UPDATE tasks SET args_override = NULL WHERE project = ? AND task_id = ?",
                (selected_proj, selected_tid),
            )

    # Re-read task row inside the same transaction so response reflects baked args.
    fresh_task = get_task(wdb, selected_proj, selected_tid)
    task_src = fresh_task if fresh_task is not None else still_queued
    claimed_row_dict = {k: task_src[k] for k in task_src.keys()}
    return run_id, finalize_token, log_p, claimed_row_dict, _lease_ttl


def claim_next_remote_task(
    db_path: Path,
    worker: sqlite3.Row,
    projects_ref: Callable[[], dict],
) -> dict:
    """Claim wrapper: guarantees the read connection is closed even when the body raises."""
    holder: list = []
    try:
        return _claim_next_remote_task(db_path, worker, projects_ref, holder)
    finally:
        for c in holder:
            try:
                c.close()
            except Exception:
                pass


def _claim_next_remote_task(
    db_path: Path,
    worker: sqlite3.Row,
    projects_ref: Callable[[], dict],
    _rdb_holder: list,
) -> dict:
    """Find and atomically claim the best queued task for this worker.

    Opens its own short-lived write connection; uses BEGIN IMMEDIATE to
    prevent double-claim races.  Returns a full response dict ready for JSON
    serialisation — always has a 'task' key (None when nothing available).

    F-059: admission predicates are revalidated inside the write transaction.
    F-063: args_override is baked in the same transaction as the claim.
    """
    from taskqueue.db import (
        failed_attempt_count, is_project_paused,
        get_config, get_project_limits, is_project_frozen, open_write_conn, with_gpu_usage,
    )
    from taskqueue.estimate import estimate_task_resources
    from taskqueue.placement import (
        apply_hold_to_node,
        clear_remote_reservation,
        refresh_remote_reservation,
        select_task_for_node,
        get_remote_reservation,
    )
    from taskqueue.scheduler import (
        FilterContext,
        build_filter_context,
        dep_status_from_conn,
        filter_dispatch,
        filter_draining,
        filter_project_allowed,
        filter_requires,
        filter_task,
        node_from_worker,
        occupancy_from_running_rows,
        worker_slot_reserved,
    )
    from taskqueue.resources import sort_queued_fairshare
    worker_id = worker["worker_id"]

    # F-002: pre-check services BEFORE acquiring write lock (see _precheck_services).
    rdb = sqlite3.connect(str(db_path), timeout=5.0)
    rdb.row_factory = sqlite3.Row
    _rdb_holder.append(rdb)
    _db_key = str(db_path)
    _cfg_at, _cfg_cached = _CONFIG_CACHE.get(_db_key, (0.0, {}))
    if not _cfg_cached or time.monotonic() - _cfg_at >= _CLAIM_CACHE_TTL:
        _cfg_cached = get_config(rdb)
        _CONFIG_CACHE[_db_key] = (time.monotonic(), _cfg_cached)
    _svc_cache = _precheck_services(rdb, _db_key, _cfg_cached)

    # F-028 / F-071: bounded candidate window — avoid materializing the full queue.
    CANDIDATE_WINDOW = 256
    from taskqueue._db_tasks import get_queued_tasks as _get_queued
    from taskqueue.db import blocked_projects as _blocked_projects
    from taskqueue.scheduler import build_task_status_map as _status_map_fn

    queued_raw = _get_queued(rdb, limit=CANDIDATE_WINDOW, statuses=("queued",))
    if len(queued_raw) >= CANDIDATE_WINDOW:
        # Head-of-line: a full window may be all dependency/paused-blocked rows.
        queued_raw = _get_queued(
            rdb,
            limit=CANDIDATE_WINDOW,
            statuses=("queued",),
            status_map=_status_map_fn(rdb),
            skip_projects=set(_blocked_projects(rdb)),
        )

    _occ_at, _occ_cached = _OCC_CACHE.get(_db_key, (0.0, []))
    if not _occ_cached or time.monotonic() - _occ_at >= _CLAIM_CACHE_TTL:
        running_rows = rdb.execute(
            "SELECT project, task_id, exclusive, exclusive_project, resources "
            "FROM tasks WHERE status = 'running'"
        ).fetchall()
        _occ_cached = running_rows
        _OCC_CACHE[_db_key] = (time.monotonic(), running_rows)
    else:
        running_rows = _occ_cached
    occ = occupancy_from_running_rows(running_rows)

    cfg = _cfg_cached
    usage_rows = rdb.execute("SELECT project, gpu_seconds FROM project_usage").fetchall()
    usage_by_proj: dict[str, float] = {r["project"]: float(r["gpu_seconds"]) for r in usage_rows}
    fairshare_weight = float(cfg.get("fairshare_weight", 1e-6))
    queued = sort_queued_fairshare(
        queued_raw, occ["gpu_running_by_proj"], usage_by_proj, fairshare_weight
    )

    peer_nodes_raw = _online_peer_nodes(rdb, cfg)

    def _resources_for(row):
        from taskqueue.rows import row_resources

        proj_name = row["project"]
        res = row_resources(row)
        _est = estimate_task_resources(rdb, proj_name, row["task_id"], res)
        cpu = float(res.get("cpu_cores", 1))
        ram = float(_est["ram_gib"])
        gpu = float(_est["vram_gib"])
        return cpu, ram, gpu

    def _hard_eligible(row, n):
        """Project/dispatch/requires/drain only — capacity checked separately for P6."""
        if not filter_draining(n).ok:
            return False
        if not filter_project_allowed(row, n).ok:
            return False
        # online_workers>=1 for remote dispatch filter
        if not filter_dispatch(row, n, FilterContext(online_workers=1)).ok:
            return False
        if not filter_requires(row, n).ok:
            return False
        return True

    hold = refresh_remote_reservation(
        queued,
        peer_nodes_raw,
        resources_for=_resources_for,
        eligible=_hard_eligible,
        cfg=cfg,
        db_key=_db_key,
    )

    res_cpu, res_ram, res_vram = worker_slot_reserved(rdb, worker_id)
    node = node_from_worker(
        worker, reserved_cpu=res_cpu, reserved_ram=res_ram, reserved_vram=res_vram
    )
    # Hold inflates capacity for everyone except the held task (applied in select loop).
    node = apply_hold_to_node(node, hold=hold)
    peer_nodes = [apply_hold_to_node(n, hold=hold) for n in peer_nodes_raw]

    from taskqueue.admit_context import AdmitProjectCaches, build_admit_context

    _admit_caches = AdmitProjectCaches()
    # Seed from precheck; miss still probes (F-138) inside build_admit_context.
    _admit_caches.svc.update(_svc_cache)

    def _ctx_for_row(row, *, conn, occupancy, config):
        return build_admit_context(
            row,
            caches=_admit_caches,
            conn=conn,
            occupancy=occupancy,
            config=config,
            dep_status=lambda p, t: dep_status_from_conn(conn, p, t),
            online_workers=1,
            running_locks=occupancy.get("running_locks") or set(),
            allow_remote=bool(config.get("notify_allow_remote")),
        )

    def _ctx_for_select(row):
        return _ctx_for_row(row, conn=rdb, occupancy=occ, config=cfg)

    placement_mode = str(cfg.get("placement_mode") or "binpack")
    placement_age = float(cfg.get("placement_age_sec", 120) or 0)
    placement_shadow = bool(int(cfg.get("placement_shadow_mode") or 0))
    prefer_last = bool(int(cfg.get("placement_prefer_last_worker", 1) or 0))

    def _log_placement(msg: str) -> None:
        import sys
        from taskqueue.scheduler import _ts
        print(f"[queue] {_ts()} info: {msg}", file=sys.stderr)

    hold_key = hold.get("key") if hold else None
    bare_claimer = node_from_worker(
        worker, reserved_cpu=res_cpu, reserved_ram=res_ram, reserved_vram=res_vram
    )

    from taskqueue.placement import last_worker_id_for_task

    def _preferred_for_row(row):
        return last_worker_id_for_task(rdb, str(row["project"]), str(row["task_id"]))

    # Per-row: held task sees bare capacity; everyone else sees hold inflation.
    selected_task = None
    selected_ram = selected_gpu = 0.0
    for row in queued:
        row_key = (str(row["project"]), str(row["task_id"]))
        use_node = bare_claimer if (hold_key and row_key == hold_key) else node
        use_peers = peer_nodes_raw if (hold_key and row_key == hold_key) else peer_nodes
        picked = select_task_for_node(
            [row],
            use_node,
            _ctx_for_select,
            peer_nodes=use_peers,
            placement_mode=placement_mode,
            placement_age_sec=placement_age,
            placement_shadow=placement_shadow,
            prefer_last_worker=prefer_last,
            preferred_for_row=_preferred_for_row if prefer_last else None,
            log_fn=_log_placement,
        )
        if picked is not None:
            selected_task = picked
            _c, selected_ram, selected_gpu = _resources_for(picked)
            break
    # Snapshot keys before closing the read connection (Row may not outlive it).
    selected_proj = selected_task["project"] if selected_task else None
    selected_tid = selected_task["task_id"] if selected_task else None
    selected_task_dict: dict | None = (
        {k: selected_task[k] for k in selected_task.keys()} if selected_task else None
    )
    rdb.close()

    _maybe_gc_orphan_packages(db_path)

    if selected_proj is None or selected_tid is None or selected_task_dict is None:
        return {"task": None}

    # F-084: capture code package BEFORE the claim commit so failure never leaves
    # a running row visible to other threads (capture must not hold BEGIN IMMEDIATE).
    try:
        code_package, package_hash = _capture_code_package(
            selected_task_dict, selected_proj, projects_ref
        )
    except Exception as exc:
        return {"task": None, "error": f"code package capture failed: {exc}"}

    # F-028/F-059: BEGIN IMMEDIATE — revalidate admission on fresh DB state.
    wdb = open_write_conn(db_path)
    wdb.execute("BEGIN IMMEDIATE")
    claimed_row = None
    claimed_row_dict: dict | None = None
    run_id = None
    finalize_token = None
    use_resume = False
    log_p = None
    try:
        _admit_caches.frozen.clear()
        _admit_caches.paused.clear()
        _admit_caches.limits.clear()
        # est cache is intentionally kept: estimates from the read pass are still
        # valid in the write pass (same tick, run history hasn't changed inside the
        # BEGIN IMMEDIATE transaction). Clearing it caused every candidate to be
        # re-estimated a second time, doubling DB round-trips.

        running_fresh = wdb.execute(
            "SELECT project, task_id, exclusive, exclusive_project, resources "
            "FROM tasks WHERE status = 'running'"
        ).fetchall()
        occ_fresh = occupancy_from_running_rows(running_fresh)
        cfg_fresh = get_config(wdb)

        worker_fresh = wdb.execute(
            "SELECT * FROM workers WHERE worker_id = ?", (worker_id,)
        ).fetchone()
        if worker_fresh is None:
            wdb.rollback()
            return {"task": None}

        res_cpu, res_ram, res_vram = worker_slot_reserved(wdb, worker_id)
        node_fresh = node_from_worker(
            worker_fresh, reserved_cpu=res_cpu, reserved_ram=res_ram, reserved_vram=res_vram
        )
        # Re-apply remote P6 hold so fillers cannot race past the read-side gate.
        _hold_fresh = get_remote_reservation(db_key=_db_key)
        _sel_key = (str(selected_proj), str(selected_tid))
        if _hold_fresh and _hold_fresh.get("key") != _sel_key:
            node_fresh = apply_hold_to_node(node_fresh, hold=_hold_fresh)

        still_queued = wdb.execute(
            "SELECT * FROM tasks WHERE project=? AND task_id=? AND status='queued' AND enabled=1",
            (selected_proj, selected_tid),
        ).fetchone()
        if not still_queued:
            wdb.rollback()
            return {"task": None}

        ctx, cpu, ram, gpu, attempts = _ctx_for_row(
            still_queued, conn=wdb, occupancy=occ_fresh, config=cfg_fresh
        )
        fr = filter_task(
            still_queued, node_fresh, ctx, cpu=cpu, ram=ram, gpu=gpu, attempt_count=attempts
        )
        if not fr.ok:
            wdb.rollback()
            return {"task": None}

        last_run = wdb.execute(
            "SELECT status FROM runs WHERE project = ? AND task_id = ? "
            "ORDER BY run_id DESC LIMIT 1",
            (selected_proj, selected_tid),
        ).fetchone()
        # F-044: match local scheduler — "stopped" also resumes
        use_resume = bool(last_run and last_run["status"] in ("interrupted", "suspended", "stopped"))

        claimed = _insert_claimed_run(
            wdb, db_path, still_queued, worker_fresh, worker_id,
            selected_proj, selected_tid, package_hash, selected_ram, selected_gpu,
        )
        if claimed is None:
            wdb.rollback()
            return {"task": None}
        run_id, finalize_token, log_p, claimed_row_dict, _lease_ttl = claimed
        wdb.commit()
        claimed_row = still_queued
        clear_remote_reservation(selected_proj, selected_tid, db_key=_db_key)
    except Exception as exc:
        wdb.rollback()
        return {"task": None, "error": str(exc)}
    finally:
        wdb.close()

    if claimed_row is None or claimed_row_dict is None:
        return {"task": None}

    resp = _claim_response(
        claimed_row_dict, projects_ref, selected_proj, run_id, log_p, use_resume,
        _lease_ttl, finalize_token,
    )
    if code_package:
        resp["code_package"] = code_package
    return resp
