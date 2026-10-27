"""Pure-ish placement helpers (PC-1…PC-5).

Scoring, best-fit gate, remote P6 hold, and soft preferred-worker tie-break.
Imports from ``scheduler`` are lazy to avoid circular import at module load.
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any, Callable

# F-140: keyed by db_path so multiple homes in one process do not share a hold.
# Mutex covers read-modify-write of each home's dict. Default key "" preserves
# single-daemon / test call sites that omit db_key.
_remote_reservation_by_db: dict[str, dict[str, Any]] = {}
_remote_reservation_lock = threading.Lock()
_DEFAULT_DB_KEY = ""


def _hold_bucket(db_key: str | None) -> dict[str, Any]:
    key = _DEFAULT_DB_KEY if db_key is None else str(db_key)
    with _remote_reservation_lock:
        return _remote_reservation_by_db.setdefault(key, {})


def score_assignment(row: Any, node: Any, ram: float, gpu: float = 0.0) -> float:
    """Higher is better. Leftover RAM after place; when gpu > 0, also leftover VRAM.

    Comparative only — not a utilization percentage.
    """
    score = node.ram_free_gib - float(ram)
    if float(gpu) > 0:
        score += node.vram_free_gib - float(gpu)
    # G6: CPU-free cores as a tie-breaker (small weight so RAM/VRAM dominate).
    cpu_free = getattr(node, "cpu_free", 0.0)
    if cpu_free > 0:
        score += float(cpu_free) * 0.01
    return score


def task_wait_aged(
    enqueued_at: str | None,
    *,
    age_sec: float,
    now: datetime | None = None,
) -> bool:
    """True when the task has waited long enough to bypass best-fit (anti-starvation)."""
    if age_sec <= 0:
        return True
    if not enqueued_at:
        return False
    try:
        from datetime import UTC

        enq = datetime.fromisoformat(str(enqueued_at))
        if enq.tzinfo is None:
            enq = enq.replace(tzinfo=UTC)
        now = now or datetime.now(UTC)
        return (now - enq).total_seconds() >= float(age_sec)
    except (TypeError, ValueError):
        return False


def claiming_worker_is_best_fit(
    claiming: Any,
    *,
    ram: float,
    gpu: float,
    peer_nodes: list[Any],
    preferred_node_id: str | None = None,
) -> tuple[bool, Any | None]:
    """Return (ok_to_claim, better_peer).

    Strictly higher score on a peer blocks. Equal scores: soft-sticky preferred
    worker wins the tie (PC-5); otherwise claim is allowed (T-031).
    """
    my_score = score_assignment(None, claiming, ram, gpu)
    better: Any | None = None
    better_score = my_score
    preferred = (preferred_node_id or "").strip() or None
    for peer in peer_nodes:
        if peer.node_id == claiming.node_id:
            continue
        s = score_assignment(None, peer, ram, gpu)
        if s > better_score + 1e-9:
            better_score = s
            better = peer
        elif (
            preferred
            and abs(s - my_score) <= 1e-9
            and peer.node_id == preferred
            and claiming.node_id != preferred
        ):
            better = peer
            better_score = s
    return (better is None, better)


def clear_remote_reservation(
    project: str | None = None,
    task_id: str | None = None,
    *,
    db_key: str | None = None,
) -> None:
    """Clear remote hold; if project/task given, only when it matches the hold key.

    With no project/task and no db_key, clears every home (test reset / full flush).
    """
    if project is None and task_id is None and db_key is None:
        with _remote_reservation_lock:
            for bucket in _remote_reservation_by_db.values():
                bucket.clear()
        return
    bucket = _hold_bucket(db_key)
    with _remote_reservation_lock:
        key = bucket.get("key")
        if project is None or key is None or key == (project, task_id):
            bucket.clear()


def get_remote_reservation(*, db_key: str | None = None) -> dict[str, Any]:
    """Return the hold for ``db_key``.

    When ``db_key`` is omitted, return the first non-empty hold (single-home /
    test convenience after a claim that keyed by absolute db path).
    """
    if db_key is not None:
        bucket = _hold_bucket(db_key)
        with _remote_reservation_lock:
            return dict(bucket)
    with _remote_reservation_lock:
        for bucket in _remote_reservation_by_db.values():
            if bucket:
                return dict(bucket)
        return dict(_remote_reservation_by_db.get(_DEFAULT_DB_KEY, {}))


def apply_hold_to_node(
    node: Any,
    *,
    hold: dict[str, Any] | None,
    skip_key: tuple[str, str] | None = None,
) -> Any:
    """Return a copy of node with remote P6 hold inflated into used capacity."""
    from taskqueue.scheduler import Node

    if not hold or hold.get("worker_id") != node.node_id:
        return node
    if skip_key is not None and hold.get("key") == skip_key:
        return node
    return Node(
        node_id=node.node_id,
        os=node.os,
        arch=node.arch,
        ram_gib=node.ram_gib,
        cpu_cores=node.cpu_cores,
        gpu_vram_gib=node.gpu_vram_gib,
        gpus=list(node.gpus),
        tags=list(node.tags),
        projects=None if node.projects is None else list(node.projects),
        is_local=node.is_local,
        ram_used_gib=float(node.ram_used_gib) + float(hold.get("ram", 0.0)),
        vram_used_gib=float(node.vram_used_gib) + float(hold.get("vram", 0.0)),
        cpu_used=node.cpu_used,
        draining=node.draining,
    )


def refresh_remote_reservation(
    queued: list[Any],
    nodes: list[Any],
    *,
    resources_for: Callable[[Any], tuple[float, float, float]],
    eligible: Callable[[Any, Any], bool],
    cfg: dict[str, Any],
    now: datetime | None = None,
    db_key: str | None = None,
) -> dict[str, Any]:
    """Maintain remote P6 hold for top fairshare task blocked on free capacity."""
    from datetime import UTC

    from taskqueue.scheduler import FilterContext, _row_get, filter_node_capacity

    now = now or datetime.now(UTC)
    max_ticks = int(cfg.get("reservation_max_ticks", 60) or 0)
    bucket = _hold_bucket(db_key)
    if max_ticks <= 0 or not queued or not nodes:
        with _remote_reservation_lock:
            bucket.clear()
        return {}

    top = queued[0]
    top_key = (str(_row_get(top, "project", "")), str(_row_get(top, "task_id", "")))
    _cpu, ram, gpu = resources_for(top)

    fitting_free = [
        n
        for n in nodes
        if eligible(top, n)
        and filter_node_capacity(
            n, FilterContext(force=False, skip_node_capacity=False), cpu=_cpu, ram=ram, gpu=gpu
        ).ok
    ]
    if fitting_free:
        with _remote_reservation_lock:
            bucket.clear()
        return {}

    roomy = [
        n
        for n in nodes
        if eligible(top, n)
        and float(n.ram_gib) + 1e-9 >= ram
        and (gpu <= 0 or float(n.gpu_vram_gib) + 1e-9 >= gpu)
    ]
    if not roomy:
        with _remote_reservation_lock:
            bucket.clear()
        return {}
    roomy.sort(key=lambda n: (float(n.ram_gib), float(n.gpu_vram_gib)), reverse=True)
    target = roomy[0]

    interval = float(cfg.get("scheduler_interval_sec", 5) or 5)
    max_sec = max_ticks * interval
    with _remote_reservation_lock:
        existing = dict(bucket)
        if existing.get("key") == top_key and existing.get("worker_id") == target.node_id:
            try:
                since = datetime.fromisoformat(str(existing["since"]))
                if since.tzinfo is None:
                    since = since.replace(tzinfo=UTC)
                if (now - since).total_seconds() > max_sec:
                    bucket.clear()
                    return {}
            except (TypeError, ValueError, KeyError):
                pass
            else:
                return existing

        bucket.clear()
        bucket.update({
            "key": top_key,
            "worker_id": target.node_id,
            "ram": float(ram),
            "vram": float(gpu),
            "since": now.isoformat(),
        })
        return dict(bucket)


def last_worker_id_for_task(conn: Any, project: str, task_id: str) -> str | None:
    """Soft preferred worker: last run's worker_id (resume / cache stickiness)."""
    row = conn.execute(
        "SELECT worker_id FROM runs WHERE project = ? AND task_id = ? "
        "AND worker_id IS NOT NULL AND worker_id != '' "
        "ORDER BY run_id DESC LIMIT 1",
        (project, task_id),
    ).fetchone()
    if row is None:
        return None
    wid = row["worker_id"] if hasattr(row, "keys") else row[0]
    return str(wid) if wid else None


def select_task_for_node(
    queued: list[Any],
    node: Any,
    ctx_for_row: Callable[[Any], tuple[Any, float, float, float, int | None]],
    *,
    peer_nodes: list[Any] | None = None,
    placement_mode: str = "binpack",
    placement_age_sec: float = 120,
    placement_shadow: bool = False,
    preferred_node_id: str | None = None,
    prefer_last_worker: bool = True,
    preferred_for_row: Callable[[Any], str | None] | None = None,
    now: datetime | None = None,
    log_fn: Callable[[str], None] | None = None,
) -> Any | None:
    """Filter then assign the first ranked feasible task (best-fit + optional sticky)."""
    from datetime import UTC

    from taskqueue.scheduler import _row_get, filter_task

    now = now or datetime.now(UTC)
    peers = list(peer_nodes or [])
    mode = (placement_mode or "binpack").strip().lower()

    for row in queued:
        ctx, cpu, ram, gpu, attempts = ctx_for_row(row)
        fr = filter_task(row, node, ctx, cpu=cpu, ram=ram, gpu=gpu, attempt_count=attempts)
        if not fr.ok:
            continue

        if mode != "binpack" or not peers:
            return row

        eligible_peers: list[Any] = []
        for peer in peers:
            if peer.node_id == node.node_id:
                continue
            pfr = filter_task(
                row, peer, ctx, cpu=cpu, ram=ram, gpu=gpu, attempt_count=attempts
            )
            if pfr.ok:
                eligible_peers.append(peer)

        pref = preferred_node_id
        if prefer_last_worker and preferred_for_row is not None:
            pref = preferred_for_row(row) or preferred_node_id

        ok, better = claiming_worker_is_best_fit(
            node,
            ram=ram,
            gpu=gpu,
            peer_nodes=eligible_peers,
            preferred_node_id=pref if prefer_last_worker else None,
        )
        if ok:
            return row

        aged = task_wait_aged(
            _row_get(row, "enqueued_at", None), age_sec=placement_age_sec, now=now
        )
        msg = (
            f"placement: skip {_row_get(row,'project','')}/{_row_get(row,'task_id','')} "
            f"on {node.node_id}; better peer {better.node_id if better else '?'}"
        )
        if placement_shadow:
            if log_fn:
                log_fn(f"SHADOW would-skip {msg}")
            return row
        if aged:
            if log_fn:
                log_fn(f"{msg} (aged — allowing)")
            return row
        if log_fn:
            log_fn(msg)
        continue

    return None
