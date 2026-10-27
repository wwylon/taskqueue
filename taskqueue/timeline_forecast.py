"""Forward-looking multi-lane Timeline forecast.

Projects queued/running work (plus cron arrivals) onto a shared time axis using
duration estimates and concurrency limits. Advisory only — not a second scheduler.
"""

from __future__ import annotations

import heapq
import json
import math
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import Any

import sqlite3

from taskqueue.cron import cron_matches
from taskqueue._db_runs import gpu_hours_today
from taskqueue._db_tasks import bulk_prior_run_seconds
from taskqueue.db import (
    blocked_projects,
    get_all_project_limits,
    get_config,
)
from taskqueue.dt_utils import iso_utc as _iso, now_utc as _now, parse_utc as _parse_ts
from taskqueue.estimate import estimate_task_resources, prefetch_exact_peaks

_UNKNOWN_SEC = 900.0  # 15 min placeholder
_SIMILAR_RATIO = 0.6
_HISTORY_RUNS = 20
_SIMILAR_PEER_CAP = 50
_CACHE_TTL_SEC = 20.0
_SUFFIX_RE = re.compile(r"(?:_seed\d+|_\d+)$", re.IGNORECASE)











def _percentile(sorted_vals: list[float], p: float) -> float | None:
    n = len(sorted_vals)
    if n == 0:
        return None
    if n == 1:
        return float(sorted_vals[0])
    idx = max(0, math.ceil((p / 100.0) * n) - 1)
    return float(sorted_vals[min(idx, n - 1)])


def _next_cron_fire(expr: str, now: datetime, *, hours: int) -> datetime | None:
    """Next cron match within ``hours``; skips whole days/hours that cannot match."""
    if not expr or not str(expr).strip():
        return None
    fields = str(expr).split()
    if len(fields) != 5:
        return None
    minute_f, hour_f, dom_f, month_f, dow_f = fields
    day_expr = f"* * {dom_f} {month_f} {dow_f}"
    hour_expr = f"* {hour_f} * * *"
    min_expr = f"{minute_f} * * * *"
    cursor = (now + timedelta(minutes=1)).replace(second=0, microsecond=0)
    end = now + timedelta(hours=hours)
    try:
        while cursor <= end:
            if not cron_matches(day_expr, cursor):
                cursor = (cursor + timedelta(days=1)).replace(hour=0, minute=0)
            elif not cron_matches(hour_expr, cursor):
                cursor = (cursor + timedelta(hours=1)).replace(minute=0)
            elif not cron_matches(min_expr, cursor):
                cursor += timedelta(minutes=1)
            else:
                return cursor
    except Exception:
        return None
    return None


def _parse_json_list(raw: Any) -> list[Any]:
    if raw is None or raw == "":
        return []
    if isinstance(raw, list):
        return raw
    try:
        val = json.loads(raw)
    except Exception:
        return []
    return val if isinstance(val, list) else []


def _parse_json_dict(raw: Any) -> dict[str, Any]:
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        val = json.loads(raw)
    except Exception:
        return {}
    return val if isinstance(val, dict) else {}


def normalize_task_id(task_id: str) -> str:
    """Strip matrix/variant suffixes like ``_seed0`` / trailing ``_12``."""
    s = str(task_id or "")
    while True:
        n = _SUFFIX_RE.sub("", s)
        if n == s:
            return s
        s = n


def tasks_similar(a: str, b: str, *, fuzzy: bool = True) -> bool:
    """True when task ids look like variants of the same job (same project assumed).

    ``fuzzy=False`` skips SequenceMatcher (expensive on large peer sets); Timeline
    uses prefix/suffix matching only when a project has many peer durations.
    """
    if not a or not b or a == b:
        return False
    na, nb = normalize_task_id(a), normalize_task_id(b)
    if na and na == nb:
        return True
    if len(na) >= 4 and len(nb) >= 4 and (na.startswith(nb) or nb.startswith(na)):
        return True
    if not fuzzy:
        return False
    return SequenceMatcher(None, a, b).ratio() >= _SIMILAR_RATIO


def _p50_from_durs(durs: list[float]) -> float | None:
    if not durs:
        return None
    return _percentile(sorted(durs), 50)


def build_duration_index(
    conn: sqlite3.Connection,
    *,
    project: str | None = None,
) -> dict[str, Any]:
    """Build per-task p50 maps for duration estimation.

    Returns ``{own: {(proj,tid): p50}, by_project: {proj: {(tid): p50}}, all_p50s: [...]}``.
    Prefers completed-run durations; falls back to all terminal if none completed.
    """
    # Last _HISTORY_RUNS finished runs per task, durations computed in SQL.
    sql = (
        "SELECT project, task_id, status, dur FROM ("
        " SELECT project, task_id, status,"
        "  (CAST(strftime('%s', finished_at) AS REAL) - CAST(strftime('%s', started_at) AS REAL)) AS dur,"
        "  ROW_NUMBER() OVER (PARTITION BY project, task_id ORDER BY run_id DESC) AS rn"
        " FROM runs WHERE finished_at IS NOT NULL AND started_at IS NOT NULL"
        " AND status IN ('completed','failed','interrupted','suspended','stopped')"
        + (" AND project = ?" if project else "")
        + f") WHERE rn <= {_HISTORY_RUNS}"
    )
    args: tuple[Any, ...] = (project,) if project else ()
    completed: dict[tuple[str, str], list[float]] = {}
    terminal: dict[tuple[str, str], list[float]] = {}
    for row in conn.execute(sql, args).fetchall():
        dur = row["dur"]
        if dur is None or dur <= 0:
            continue
        key = (row["project"] or "", row["task_id"] or "")
        terminal.setdefault(key, []).append(float(dur))
        if row["status"] == "completed":
            completed.setdefault(key, []).append(float(dur))

    own: dict[tuple[str, str], float] = {}
    by_project: dict[str, dict[str, float]] = {}
    all_p50s: list[float] = []
    keys = set(terminal) | set(completed)
    for key in keys:
        durs = completed.get(key) or terminal.get(key) or []
        p50 = _p50_from_durs(durs)
        if p50 is None:
            continue
        own[key] = float(p50)
        all_p50s.append(float(p50))
        by_project.setdefault(key[0], {})[key[1]] = float(p50)
    return {"own": own, "by_project": by_project, "all_p50s": all_p50s}


def estimate_duration_sec(
    project: str,
    task_id: str,
    index: dict[str, Any],
) -> tuple[float, str]:
    """Cascade: own history → similar names → project avg → global avg → unknown placeholder."""
    own: dict[tuple[str, str], float] = index["own"]
    key = (project, task_id)
    if key in own:
        return own[key], "history"

    memo: dict = index.setdefault("_sim_memo", {})
    mkey = (project, normalize_task_id(task_id))
    if mkey in memo:
        hit = memo[mkey]
    else:
        peers = index["by_project"].get(project) or {}
        # SequenceMatcher is O(n*m) per pair — skip fuzzy match on large peer sets.
        fuzzy = len(peers) <= 200
        similar_vals = []
        for n, (tid, v) in enumerate(peers.items()):
            if n >= _SIMILAR_PEER_CAP * 20:
                break
            if tid != task_id and tasks_similar(task_id, tid, fuzzy=fuzzy):
                similar_vals.append(v)
                if len(similar_vals) >= _SIMILAR_PEER_CAP:
                    break
        hit = (sum(similar_vals) / len(similar_vals)) if similar_vals else None
        memo[mkey] = hit
    if hit is not None:
        return hit, "similar"

    proj_vals = list((index["by_project"].get(project) or {}).values())
    if proj_vals:
        return sum(proj_vals) / len(proj_vals), "project_avg"

    all_p50s: list[float] = index["all_p50s"]
    if all_p50s:
        return sum(all_p50s) / len(all_p50s), "global_avg"

    return _UNKNOWN_SEC, "unknown"


def _dep_key(ref: Any, default_project: str) -> tuple[str, str]:
    s = str(ref)
    if "/" in s:
        proj, tid = s.split("/", 1)
        return proj, tid
    return default_project, s


@dataclass
class _Cand:
    project: str
    task_id: str
    status: str
    kind: str  # running | queued | cron
    priority: int = 0
    enqueued_at: str = ""
    depends_on: list[Any] = field(default_factory=list)
    depends_on_any: list[Any] = field(default_factory=list)
    exclusive: bool = False
    exclusive_project: bool = False
    cpu: float = 1.0
    ram: float = 1.0
    gpu: float = 0.0
    duration_sec: float = _UNKNOWN_SEC
    estimate_source: str = "unknown"
    remaining_sec: float = _UNKNOWN_SEC
    arrive_sec: float = 0.0


@dataclass
class _Active:
    cand: _Cand
    start_sec: float
    end_sec: float
    lane: int


def _load_candidates(
    conn: sqlite3.Connection,
    *,
    project: str | None,
    hours: int,
    now: datetime,
    index: dict[str, Any],
) -> tuple[list[_Cand], set[tuple[str, str]]]:
    """Return (candidates, already-completed keys from DB)."""
    completed: set[tuple[str, str]] = set()
    for row in conn.execute(
        "SELECT project, task_id, status FROM tasks"
        + (" WHERE project = ?" if project else ""),
        (project,) if project else (),
    ).fetchall():
        if row["status"] == "completed":
            completed.add((row["project"] or "", row["task_id"] or ""))

    sql = (
        "SELECT project, task_id, status, priority, enqueued_at, depends_on, depends_on_any, "
        "exclusive, exclusive_project, resources, schedule, enabled "
        "FROM tasks WHERE status IN ('running','queued')"
    )
    args: list[Any] = []
    if project:
        sql += " AND project = ?"
        args.append(project)

    cands: list[_Cand] = []
    seen: set[tuple[str, str]] = set()
    cfg = get_config(conn)
    default_ram = float(cfg.get("default_task_ram_gib") or 1.0)
    predict_runs = int(cfg.get("resource_predict_runs") or 5)
    est_cache: dict = {}
    open_starts: dict[tuple[str, str], str] = {}
    open_runs: dict[tuple[str, str], Any] = {}
    open_sql = (
        "SELECT run_id, project, task_id, started_at, progress_pct FROM runs "
        "WHERE finished_at IS NULL"
    )
    open_args: list[Any] = []
    if project:
        open_sql += " AND project = ?"
        open_args.append(project)
    open_sql += " ORDER BY run_id ASC"
    for r in conn.execute(open_sql, open_args).fetchall():
        open_starts[(r["project"] or "", r["task_id"] or "")] = r["started_at"]
        open_runs[(r["project"] or "", r["task_id"] or "")] = r
    prior_secs = bulk_prior_run_seconds(conn, open_runs)

    blocked = set(blocked_projects(conn))

    live_rows = list(conn.execute(sql, args).fetchall())
    # Cron arrivals within the forward window
    cron_sql = (
        "SELECT project, task_id, status, priority, enqueued_at, depends_on, depends_on_any, "
        "exclusive, exclusive_project, resources, schedule, enabled "
        "FROM tasks WHERE schedule IS NOT NULL AND schedule != '' AND schedule != 'null' "
        "AND enabled = 1"
    )
    cron_args: list[Any] = []
    if project:
        cron_sql += " AND project = ?"
        cron_args.append(project)
    cron_rows = list(conn.execute(cron_sql, cron_args).fetchall())

    # Bulk exact-task resource peaks for every candidate we might emit.
    est_keys: list[tuple[str, str]] = []
    for row in live_rows:
        est_keys.append((row["project"] or "", row["task_id"] or ""))
    for row in cron_rows:
        est_keys.append((row["project"] or "", row["task_id"] or ""))
    exact_peaks, exact_checked = prefetch_exact_peaks(conn, est_keys, predict_runs=predict_runs)

    def _est(proj: str, tid: str, res: dict[str, Any]) -> dict[str, Any]:
        return estimate_task_resources(
            conn, proj, tid, res,
            default_ram_gib=default_ram,
            predict_runs=predict_runs,
            cache=est_cache,
            exact_peaks=exact_peaks,
            exact_checked=exact_checked,
        )

    for row in live_rows:
        proj = row["project"] or ""
        tid = row["task_id"] or ""
        key = (proj, tid)
        seen.add(key)
        # Paused/frozen projects admit nothing new; only already-running tasks matter.
        if row["status"] != "running" and proj in blocked:
            continue
        res = _parse_json_dict(row["resources"])
        est = _est(proj, tid, res)
        dur, src = estimate_duration_sec(proj, tid, index)
        remaining = dur
        if row["status"] == "running":
            started = _parse_ts(open_starts.get(key)) if open_starts.get(key) else None
            if started is not None:
                run = open_runs.get(key)
                elapsed = max(0.0, (now - started).total_seconds())
                if run is not None:
                    # Earlier runs this one resumed count toward the whole running time.
                    elapsed += prior_secs.get(key, 0.0)
                pct = run["progress_pct"] if run is not None else None
                if pct is not None and 5.0 <= float(pct) < 100.0:
                    remaining = max(1.0, elapsed * (100.0 - float(pct)) / float(pct))
                    src = "progress"
                    dur = elapsed + remaining
                elif elapsed >= dur:
                    # Past the estimate: assume a short tail instead of a 1 s sliver.
                    remaining = max(60.0, 0.1 * dur)
                    src = "overdue"
                else:
                    remaining = max(1.0, dur - elapsed)
        cands.append(_Cand(
            project=proj,
            task_id=tid,
            status=row["status"] or "queued",
            kind="running" if row["status"] == "running" else "queued",
            priority=int(row["priority"] or 0),
            enqueued_at=str(row["enqueued_at"] or ""),
            depends_on=_parse_json_list(row["depends_on"]),
            depends_on_any=_parse_json_list(row["depends_on_any"]),
            exclusive=bool(row["exclusive"]),
            exclusive_project=bool(row["exclusive_project"]),
            cpu=float(est["cpu_cores"]),
            ram=float(est["ram_gib"]),
            gpu=float(est["vram_gib"]),
            duration_sec=dur,
            estimate_source=src,
            remaining_sec=remaining,
            arrive_sec=0.0,
        ))

    span = float(hours * 3600)
    for row in cron_rows:
        proj = row["project"] or ""
        tid = row["task_id"] or ""
        key = (proj, tid)
        if key in seen or proj in blocked:
            continue
        sched = _parse_json_dict(row["schedule"])
        cron_expr = sched.get("cron")
        if not cron_expr:
            continue
        nxt = _next_cron_fire(str(cron_expr), now, hours=hours)
        if nxt is None:
            continue
        arrive = (nxt - now).total_seconds()
        if arrive < 0 or arrive > span:
            continue
        res = _parse_json_dict(row["resources"])
        est = _est(proj, tid, res)
        dur, src = estimate_duration_sec(proj, tid, index)
        cands.append(_Cand(
            project=proj,
            task_id=tid,
            status=row["status"] or "queued",
            kind="cron",
            priority=int(row["priority"] or 0),
            enqueued_at=str(row["enqueued_at"] or ""),
            depends_on=_parse_json_list(row["depends_on"]),
            depends_on_any=_parse_json_list(row["depends_on_any"]),
            exclusive=bool(row["exclusive"]),
            exclusive_project=bool(row["exclusive_project"]),
            cpu=float(est["cpu_cores"]),
            ram=float(est["ram_gib"]),
            gpu=float(est["vram_gib"]),
            duration_sec=dur,
            estimate_source=src,
            remaining_sec=dur,
            arrive_sec=arrive,
        ))
        seen.add(key)

    return cands, completed


def _deps_ok(cand: _Cand, completed: set[tuple[str, str]]) -> bool:
    if cand.depends_on:
        for ref in cand.depends_on:
            if _dep_key(ref, cand.project) not in completed:
                return False
    if cand.depends_on_any:
        if not any(_dep_key(ref, cand.project) in completed for ref in cand.depends_on_any):
            return False
    return True


def _global_task_cap(cfg: dict[str, Any]) -> int:
    caps = []
    for key in ("max_running_tasks", "max_concurrent_tasks"):
        v = int(cfg.get(key) or 0)
        if v > 0:
            caps.append(v)
    return min(caps) if caps else 0


def forecast_timeline(
    conn: sqlite3.Connection,
    *,
    hours: int = 24,
    project: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Project future runs onto ``[now, now+hours]`` with multi-lane packing."""
    hours = max(1, min(int(hours), 168))
    t0 = _now(now)
    span = float(hours * 3600)
    window_end = t0 + timedelta(hours=hours)

    index = build_duration_index(conn, project=project)
    candidates, completed = _load_candidates(
        conn, project=project, hours=hours, now=t0, index=index,
    )

    cfg = get_config(conn)
    g_max_tasks = _global_task_cap(cfg)
    g_max_cpu = float(cfg.get("max_cpu_cores") or 0)
    g_max_ram = float(cfg.get("max_ram_gib") or 0)
    all_plim = get_all_project_limits(conn)
    gpu_day_used: dict[str, float] = {
        p: float(gpu_hours_today(conn, p))
        for p, lim in all_plim.items()
        if float((lim or {}).get("max_gpu_hours_day", 0) or 0) > 0
    }

    # Sort pending (non-running) by fairshare-ish key
    running_now = [c for c in candidates if c.kind == "running"]
    pending = [c for c in candidates if c.kind != "running"]
    # Sorted once: a single in-order pass per event time is equivalent to
    # restarting the scan after each start, because starting a task can only
    # tighten the limits for candidates already rejected at the same instant.
    pending.sort(key=lambda c: (-c.priority, c.arrive_sec, c.enqueued_at or "", c.project, c.task_id))
    # Lazy arrival heap: cron (arrive_sec > 0) only; stale entries skipped after pop.
    arrive_heap: list[tuple[float, tuple[str, str]]] = [
        (c.arrive_sec, (c.project, c.task_id)) for c in pending if c.arrive_sec > 1e-9
    ]
    heapq.heapify(arrive_heap)
    pending_keys = {(c.project, c.task_id) for c in pending}

    active: dict[tuple[str, str], _Active] = {}
    finish_heap: list[tuple[float, int, tuple[str, str]]] = []
    seq = 0
    used_lanes: set[int] = set()
    bars: list[dict[str, Any]] = []
    raw_bunch_end = 0.0

    n_by_proj: dict[str, int] = {}
    cpu_by_proj: dict[str, float] = {}
    ram_by_proj: dict[str, float] = {}
    gpu_by_proj: dict[str, float] = {}
    total_cpu = 0.0
    total_ram = 0.0
    has_exclusive = False
    exclusive_projects: set[str] = set()

    def _alloc_lane() -> int:
        lane = 0
        while lane in used_lanes:
            lane += 1
        used_lanes.add(lane)
        return lane

    def _start(cand: _Cand, t: float) -> None:
        nonlocal seq, total_cpu, total_ram, has_exclusive
        key = (cand.project, cand.task_id)
        if key in active:
            return
        dur = cand.remaining_sec if (cand.kind == "running" and t <= 1e-9) else cand.duration_sec
        end = t + max(1.0, dur)
        lane = _alloc_lane()
        act = _Active(cand=cand, start_sec=t, end_sec=end, lane=lane)
        active[key] = act
        seq += 1
        heapq.heappush(finish_heap, (end, seq, key))
        n_by_proj[cand.project] = n_by_proj.get(cand.project, 0) + 1
        cpu_by_proj[cand.project] = cpu_by_proj.get(cand.project, 0.0) + cand.cpu
        ram_by_proj[cand.project] = ram_by_proj.get(cand.project, 0.0) + cand.ram
        gpu_by_proj[cand.project] = gpu_by_proj.get(cand.project, 0.0) + cand.gpu
        total_cpu += cand.cpu
        total_ram += cand.ram
        if cand.exclusive:
            has_exclusive = True
        if cand.exclusive_project:
            exclusive_projects.add(cand.project)

    def _finish(key: tuple[str, str]) -> None:
        nonlocal total_cpu, total_ram, has_exclusive, raw_bunch_end
        act = active.pop(key, None)
        if act is None:
            return
        cand = act.cand
        used_lanes.discard(act.lane)
        n_by_proj[cand.project] = max(0, n_by_proj.get(cand.project, 0) - 1)
        cpu_by_proj[cand.project] = max(0.0, cpu_by_proj.get(cand.project, 0.0) - cand.cpu)
        ram_by_proj[cand.project] = max(0.0, ram_by_proj.get(cand.project, 0.0) - cand.ram)
        gpu_by_proj[cand.project] = max(0.0, gpu_by_proj.get(cand.project, 0.0) - cand.gpu)
        total_cpu = max(0.0, total_cpu - cand.cpu)
        total_ram = max(0.0, total_ram - cand.ram)
        if cand.exclusive:
            has_exclusive = any(a.cand.exclusive for a in active.values())
        if cand.exclusive_project and cand.project in exclusive_projects:
            if not any(
                a.cand.exclusive_project and a.cand.project == cand.project
                for a in active.values()
            ):
                exclusive_projects.discard(cand.project)
        completed.add(key)
        raw_bunch_end = max(raw_bunch_end, act.end_sec)
        if cand.gpu > 0 and cand.project in gpu_day_used and act.start_sec > 1e-9:
            gpu_day_used[cand.project] += (act.end_sec - act.start_sec) / 3600.0

        if act.start_sec >= span:
            return
        clip_s = max(0.0, act.start_sec)
        clip_e = min(span, act.end_sec)
        if clip_e <= clip_s:
            return
        bars.append({
            "project": cand.project,
            "task_id": cand.task_id,
            "status": "running" if cand.kind == "running" else "queued",
            "start_sec": round(clip_s, 1),
            "end_sec": round(clip_e, 1),
            "lane": act.lane,
            "duration_sec": round(cand.duration_sec, 1),
            "estimate_source": cand.estimate_source,
            "kind": cand.kind,
            "priority": cand.priority,
            "clipped": bool(act.end_sec > span),
            "start_at": _iso(t0 + timedelta(seconds=clip_s)),
            "end_at": _iso(t0 + timedelta(seconds=act.end_sec)),
        })

    def _can_start(cand: _Cand) -> bool:
        if not _deps_ok(cand, completed):
            return False
        n_running = len(active)
        if cand.exclusive and n_running > 0:
            return False
        if has_exclusive:
            return False
        if cand.exclusive_project and n_by_proj.get(cand.project, 0) > 0:
            return False
        if cand.project in exclusive_projects:
            return False
        if g_max_tasks > 0 and n_running >= g_max_tasks:
            return False
        if g_max_cpu > 0 and total_cpu + cand.cpu > g_max_cpu:
            return False
        if g_max_ram > 0 and total_ram + cand.ram > g_max_ram:
            return False
        plim = all_plim.get(cand.project) or {}
        p_max = int(plim.get("max_tasks", 0) or 0)
        if p_max > 0 and n_by_proj.get(cand.project, 0) >= p_max:
            return False
        p_cpu = float(plim.get("max_cpu_cores", 0) or 0)
        if p_cpu > 0 and cpu_by_proj.get(cand.project, 0.0) + cand.cpu > p_cpu:
            return False
        p_ram = float(plim.get("max_ram_gib", 0) or 0)
        if p_ram > 0 and ram_by_proj.get(cand.project, 0.0) + cand.ram > p_ram:
            return False
        p_gpu_day = float(plim.get("max_gpu_hours_day", 0) or 0)
        if cand.gpu > 0 and p_gpu_day > 0 and gpu_day_used.get(cand.project, 0.0) >= p_gpu_day:
            return False
        p_gpu = float(plim.get("max_gpu_vram_gib", 0) or 0)
        if p_gpu > 0 and gpu_by_proj.get(cand.project, 0.0) + cand.gpu > p_gpu:
            return False
        return True

    # Seed currently running at t=0
    for cand in running_now:
        _start(cand, 0.0)

    t = 0.0
    max_steps = max(100, len(candidates) * 4 + 10)
    steps = 0
    while steps < max_steps:
        steps += 1
        still: list[_Cand] = []
        for idx, cand in enumerate(pending):
            if g_max_tasks > 0 and len(active) >= g_max_tasks:
                # Global cap full: nothing else can start at this instant.
                still.extend(c for c in pending[idx:] if (c.project, c.task_id) not in active)
                break
            if cand.arrive_sec > t + 1e-9:
                still.append(cand)
                continue
            if (cand.project, cand.task_id) in active:
                continue
            if _can_start(cand):
                _start(cand, t)
            else:
                still.append(cand)
        pending = still
        pending_keys = {(c.project, c.task_id) for c in pending}

        if not pending and not active:
            break

        next_finish = finish_heap[0][0] if finish_heap else None
        next_arrive = None
        while arrive_heap:
            at, akey = arrive_heap[0]
            if akey not in pending_keys:
                heapq.heappop(arrive_heap)
                continue
            if at <= t + 1e-9:
                heapq.heappop(arrive_heap)
                continue
            next_arrive = at
            break

        candidates_t = [x for x in (next_finish, next_arrive) if x is not None]
        if not candidates_t:
            break
        t_next = min(candidates_t)

        if next_finish is not None and abs(next_finish - t_next) < 1e-9:
            t = next_finish
            while finish_heap and finish_heap[0][0] <= t + 1e-9:
                _, _, key = heapq.heappop(finish_heap)
                if key in active:
                    _finish(key)
        else:
            t = t_next

        # Stop admitting far past the window once nothing active in-window
        if t > span and not active:
            break
        if t > span * 3:
            break

    for key in list(active.keys()):
        _finish(key)

    bars.sort(key=lambda b: (b["start_sec"], b["lane"], b["project"], b["task_id"]))
    for i, bar in enumerate(bars, start=1):
        bar["order"] = i
    return {
        "hours": hours,
        "window_start": _iso(t0),
        "window_end": _iso(window_end),
        "span_sec": span,
        "bunch_end_sec": round(raw_bunch_end, 1),
        "bunch_end_at": _iso(t0 + timedelta(seconds=raw_bunch_end)) if raw_bunch_end > 0 else None,
        "bars": bars,
    }


def forecast_order_map(
    conn: sqlite3.Connection,
    *,
    hours: int = 24,
    project: str | None = None,
    now: datetime | None = None,
) -> dict[tuple[str, str], dict[str, float | int]]:
    """Map (project, task_id) → {order, start_sec} from the timeline forecast."""
    data = cached_forecast(conn, hours=hours, project=project, now=now)
    out: dict[tuple[str, str], dict[str, float | int]] = {}
    for bar in data.get("bars") or []:
        key = (str(bar.get("project") or ""), str(bar.get("task_id") or ""))
        if not key[0] or not key[1]:
            continue
        out[key] = {
            "order": int(bar.get("order") or 0),
            "start_sec": float(bar.get("start_sec") or 0.0),
        }
    return out


_cache: dict[tuple[str, int], tuple[float, tuple, dict[str, Any]]] = {}
_cache_lock = threading.Lock()


def _state_token(conn: sqlite3.Connection, project: str | None) -> tuple:
    """Cheap fingerprint of everything the forecast depends on."""
    w = " WHERE project = ?" if project else ""
    a = (project,) if project else ()
    rows = conn.execute(
        "SELECT status, COUNT(*), COALESCE(SUM(priority),0), COALESCE(SUM(enabled),0), "
        "MAX(enqueued_at) FROM tasks" + w + " GROUP BY status", a,
    ).fetchall()
    run_w = " WHERE project = ?" if project else ""
    last_run = conn.execute("SELECT MAX(run_id) FROM runs" + run_w, a).fetchone()[0]
    blocked = tuple(sorted(
        r["key"] for r in conn.execute(
            "SELECT key FROM config WHERE key LIKE 'project_paused_%' "
            "OR key LIKE 'project_frozen_%'"
        ).fetchall()
    ))
    return (tuple(tuple(r) for r in rows), last_run, blocked)


def cached_forecast(
    conn: sqlite3.Connection,
    *,
    hours: int = 24,
    project: str | None = None,
    now: datetime | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """TTL + state-token cached ``forecast_timeline``; concurrent callers share one compute."""
    hours = max(1, min(int(hours), 168))
    if now is not None:
        data = forecast_timeline(conn, hours=hours, project=project, now=now)
        return _finish(data, limit, 0.0, False, 0.0)
    key = (project or "", hours)
    token = _state_token(conn, project)
    mono = time.monotonic()
    hit = _cache.get(key)
    if hit and mono - hit[0] < _CACHE_TTL_SEC and hit[1] == token:
        return _finish(hit[2], limit, hit[2].get("compute_ms", 0.0), True, mono - hit[0])
    with _cache_lock:
        hit = _cache.get(key)
        mono = time.monotonic()
        if hit and mono - hit[0] < _CACHE_TTL_SEC and hit[1] == token:
            return _finish(hit[2], limit, hit[2].get("compute_ms", 0.0), True, mono - hit[0])
        t_start = time.monotonic()
        data = forecast_timeline(conn, hours=hours, project=project)
        data["compute_ms"] = round((time.monotonic() - t_start) * 1000.0, 1)
        _cache[key] = (time.monotonic(), token, data)
        return _finish(data, limit, data["compute_ms"], False, 0.0)


def _finish(data: dict[str, Any], limit: int | None, compute_ms: float, cached: bool, age: float) -> dict[str, Any]:
    out = dict(data)
    bars = data.get("bars") or []
    out["total_bars"] = len(bars)
    out["truncated"] = bool(limit and len(bars) > limit)
    if limit and len(bars) > limit:
        out["bars"] = bars[:limit]
    out["compute_ms"] = compute_ms
    out["cached"] = cached
    out["age_sec"] = round(age, 1)
    return out
