"""RAM / VRAM hard-pressure stops (split from daemon.py; daemon re-exports these names)."""

from __future__ import annotations

from dataclasses import dataclass

from tqdaemon import errors as tqerr
from tqdaemon._db_tasks import set_task_error
from tqdaemon._time import (
    _ts,  # noqa: E402 (after local imports)
)
from tqdaemon.lease import (  # noqa: F401  (re-exported for callers/tests)
    MasterLease,
    MasterLeaseLockTimeout,
    _acquire_master_lease,
    _cas_master_lease,
    _start_lease_renew_thread,
)
from tqdaemon.models import (
    task_key,
)
from tqdaemon.resources import ram_used_gib
from tqdaemon.victim import pick_victim
from tqdaemon.yaml_sync import (  # noqa: F401
    _YAML_CACHE_MAX,
    YAML_PARSER_GEN,
    _sync_project,
    _yaml_mtime_cache,
    clear_yaml_mtime_cache,
    iter_queue_files,
)


def _split_dying(active_runs: dict, attr: str) -> tuple[dict, float]:
    """Split runs already being stopped from live eviction candidates.

    A run with ``stop_reason`` set is on its way out: re-picking it burns the
    per-tick stop budget without freeing anything new, so it is excluded and its
    reading (*attr*) is returned as already-pending relief.
    """
    live: dict = {}
    pending = 0.0
    for k, r in active_runs.items():
        if getattr(r, "stop_reason", None):
            pending += float(getattr(r, attr, 0.0) or 0.0)
        else:
            live[k] = r
    return live, pending


@dataclass(frozen=True)
class PressureSpec:
    """One hard-pressure resource (host RAM or GPU VRAM) and how its stops are worded."""

    resource: str  # pick_victim resource
    attr: str  # ActiveRun reading
    stop_reason: str
    err_code: str
    label: str  # "RAM" / "VRAM"
    used_prefix: str  # "host" / "VRAM" (log line)
    block_prefix: str  # "RAM: host" / "VRAM: device" (block_reason)
    held_word: str  # "RSS" / "VRAM"


RAM_PRESSURE = PressureSpec("ram", "current_ram_gib", "ram_hard", tqerr.STOP_RAM, "RAM", "host", "RAM: host", "RSS")


VRAM_PRESSURE = PressureSpec(
    "vram", "current_vram_gib", "vram_hard", tqerr.STOP_VRAM, "VRAM", "VRAM", "VRAM: device", "VRAM"
)


def _stop_pressure_victims(
    conn,
    remaining: dict,
    spec: PressureSpec,
    *,
    used: float,
    projected: float,
    soft_limit: float,
    hard_limit: float,
    total: float,
    stop_cap: int,
) -> int:
    """Stop victims (cheapest to kill first) until ``projected`` drops under ``soft_limit``.

    Shared by the hard-RAM and hard-VRAM paths. ``remaining`` holds runs not already dying and
    is consumed. Reclaim is asynchronous, so usage is projected rather than re-read. Returns
    how many runs were stopped (at most ``stop_cap``).
    """
    stopped = 0
    while projected > soft_limit and remaining and stopped < stop_cap:
        # D7: pick by cost-of-killing, not just biggest RSS, so an idempotent task dies
        # before a non-idempotent one that is nearly finished.
        victim_key, scored = pick_victim(
            conn,
            list(remaining.items()),
            needed_gib=max(0.0, projected - soft_limit),
            resource=spec.resource,
        )
        if victim_key is None:
            break
        victim = remaining.pop(victim_key)
        held = float(getattr(victim, spec.attr) or 0.0)
        runner_up = f"; runner-up {task_key(*scored[1][0])} score={scored[1][1]:.2f}" if len(scored) > 1 else ""
        victim.stop_reason = spec.stop_reason
        print(
            f"[queue] {_ts()} {spec.label} hard limit: stopping {task_key(*victim_key)} "
            f"({spec.used_prefix} {used:.1f} > hard {hard_limit:.1f}; "
            f"task {spec.held_word} {held:.2f} GiB; "
            f"score={scored[0][1]:.2f}{runner_up})",
            flush=True,
        )
        set_task_error(
            conn,
            victim_key[0],
            victim_key[1],
            spec.err_code,
            block_reason=(
                f"{spec.block_prefix} {used:.1f} > hard {hard_limit:.1f} "
                f"(soft {soft_limit:.1f}, total {total:.1f}); "
                f"victim {spec.held_word} {held:.2f} GiB"
            ),
        )
        victim.stop()
        projected -= held
        stopped += 1
    return stopped


def _pressure_used(spec: PressureSpec, system_info: dict, active_runs: dict, *, refreshed: bool) -> float:
    """Current usage for *spec*: host RAM, or device VRAM (task sum as fallback / after refresh)."""
    if spec is RAM_PRESSURE:
        return ram_used_gib()
    if refreshed:
        return sum(float(r.current_vram_gib or 0.0) for r in active_runs.values())
    # F-143: prefer host-wide GPU usage (includes non-task CUDA) over task-only sum.
    used = sum(float(g.get("vram_used_gib") or 0.0) for g in (system_info.get("gpus") or []))
    return used if used > 0 else sum(float(r.current_vram_gib or 0.0) for r in active_runs.values())


def _pressure_limits(spec: PressureSpec, cfg: dict, system_info: dict) -> tuple[float, float, float]:
    """(total, hard_limit, soft_limit) GiB for *spec*; total 0 disables the check."""
    if spec is RAM_PRESSURE:
        total = float(system_info.get("ram_total_gib") or 0.0)
        soft_pct = float(cfg.get("ram_limit_pct", 80))
        hard_pct = max(float(cfg.get("ram_hard_limit_pct", 95)), soft_pct)
    else:
        hard_pct = float(cfg.get("vram_hard_limit_pct", 95))
        total = float(system_info.get("gpu_vram_total_gib") or 0.0) if hard_pct > 0 else 0.0
        soft_pct = float(cfg.get("gpu_limit_pct", 100))
    return total, total * hard_pct / 100.0, total * soft_pct / 100.0


def _enforce_hard_pressure(conn, cfg: dict, system_info: dict, active_runs: dict, now: float) -> float | None:
    """Hard RAM / VRAM response: stop victims among local runs.

    Local processes consume host memory regardless of master-lease or sleep
    state, so this is also called while the lease is lost.  Returns the new
    ``admit_frozen_until`` (monotonic) when a victim was stopped, else None.
    """
    admit_frozen_until: float | None = None
    for spec in (RAM_PRESSURE, VRAM_PRESSURE):
        if not active_runs:
            break
        total, hard_limit, soft_limit = _pressure_limits(spec, cfg, system_info)
        if total <= 0 or hard_limit <= 0:
            continue
        used = _pressure_used(spec, system_info, active_runs, refreshed=False)
        if used <= hard_limit:
            continue
        # Refresh readings before choosing victims (VRAM: only under pressure, nvidia-smi is slow).
        for run in list(active_runs.values()):
            run.update_resources()
        if spec is VRAM_PRESSURE:
            used = _pressure_used(spec, system_info, active_runs, refreshed=True)
        # D9: stop until projected usage is back under the SOFT limit, not merely under
        # hard — stopping just enough to clear hard leaves the host one allocation away
        # from thrashing again.
        remaining, dying = _split_dying(active_runs, spec.attr)
        if _stop_pressure_victims(
            conn,
            remaining,
            spec,
            used=used,
            projected=used - dying,
            soft_limit=soft_limit,
            hard_limit=hard_limit,
            total=total,
            stop_cap=int(cfg.get("stop_max_per_tick", 2)),
        ):
            # D9: freeze admission while the OS reclaims, or admit later in this tick
            # sizes the next task against memory the victim has not released (F7).
            admit_frozen_until = now + float(cfg.get("recovery_cooldown_sec", 30))
    return admit_frozen_until
