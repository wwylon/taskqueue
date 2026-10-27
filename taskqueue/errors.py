"""Stable taskqueue error codes (tq.*). See docs/errors.md."""

from __future__ import annotations

# Blocked from starting (task remains queued / waiting)
BLOCK_DEPS = "tq.block.deps"
BLOCK_RESOURCES = "tq.block.resources"
BLOCK_EXCLUSIVE = "tq.block.exclusive"
BLOCK_EXCLUSIVE_PROJECT = "tq.block.exclusive_project"
BLOCK_REQUIRES_PATHS = "tq.block.requires_paths"
BLOCK_VRAM_EXCEEDS_SYSTEM = "tq.block.vram_exceeds_system"
BLOCK_SERVICE = "tq.block.service_down"   # R1: requires_services health check failed
BLOCK_SERVICE_VRAM = "tq.block.service_vram"  # S1: service up but VRAM headroom insufficient
BLOCK_MUTEX = "tq.block.mutex"            # S2: named resource lock held by another running task
BLOCK_SCAVENGER = "tq.block.scavenger"  # T-204: scavenger waits while other work contends
BLOCK_MAX_ATTEMPTS = "tq.block.max_attempts"  # F-099: max_attempts exhausted

# Run / process outcomes
RUN_NONZERO = "tq.run.nonzero"
RUN_CAP = "tq.run.cap"
RUN_PREFLIGHT = "tq.run.preflight"
RUN_FROZEN = "tq.run.frozen"
RUN_PROGRESS_SUSPECT = "tq.run.progress_suspect"
STOP_RAM = "tq.stop.ram"  # host over hard RAM limit; status=stopped
STOP_VRAM = "tq.stop.vram"  # GPU over hard VRAM limit; status=stopped
DEP_CANCELLED = "tq.dep.cancelled"  # T-206: cancelled because a dependency failed (on_dependency_failure: cancel)
STOP_SCAVENGER = "tq.stop.scavenger"  # T-204: preempted for higher-priority work; status=stopped (auto-restart)
STOP_DEPS = "tq.stop.deps"  # depends_on peer no longer completed while running; status=stopped

RUN_ARTIFACTS_MISSING = "tq.run.artifacts_missing"
RUN_NEGATIVE = "tq.run.negative"  # exit code listed in negative_exit_codes: completed, answer is "no"
DEP_NEGATIVE = "tq.dep.negative"  # cancelled: a dependency ended negative (on_dependency_negative: cancel)

# Infrastructure
INFRA_WORKER_LOST = "tq.infra.worker_lost"
INFRA_CLOCK_JUMP = "tq.infra.clock_jump"
INFRA_CLOCK_JUMP_FLAP = "tq.infra.clock_jump_flap"
INFRA_LEASE_EXPIRED_WORKER_ALIVE = "tq.infra.lease_expired_worker_alive"
INFRA_HUNG = "tq.infra.hung"
INFRA_ORPHAN = "tq.infra.orphan"

_DESCRIPTIONS: dict[str, str] = {
    BLOCK_DEPS: "Dependency tasks not completed successfully",
    BLOCK_RESOURCES: "Host over soft RAM / CPU/GPU budget, or anti-flap (see block_reason)",
    BLOCK_EXCLUSIVE: "Blocked by exclusive task gating",
    BLOCK_EXCLUSIVE_PROJECT: "Blocked by per-project exclusive task gating",
    BLOCK_REQUIRES_PATHS: "required_paths missing on the execution host",
    BLOCK_VRAM_EXCEEDS_SYSTEM: "Task requests more VRAM than the system's GPU total — YAML must be corrected",
    BLOCK_SERVICE: "A declared requires_services health check failed — service is down, unreachable, or a probe timed out",
    BLOCK_SERVICE_VRAM: "Service is up but GPU VRAM headroom is insufficient — resident model weights still loaded",
    BLOCK_MUTEX: "Named resource lock held by another running task — waiting for exclusive access",
    BLOCK_MAX_ATTEMPTS: "max_attempts exhausted — use tq patch … max_attempts=0 then tq retry to unstick",
    BLOCK_SCAVENGER: "Scavenger task waits: other (non-scavenger) work is waiting on capacity",
    RUN_NONZERO: "Process exited with a non-zero status",
    RUN_CAP: "Stopped after wall_clock_cap_sec",
    RUN_PREFLIGHT: "Command or required path missing before start",
    RUN_FROZEN: "No stdout and no process CPU for ≥2× frozen_threshold_sec",
    RUN_PROGRESS_SUSPECT: "Progress % advanced without matching process CPU time",
    RUN_ARTIFACTS_MISSING: "One or more declared output artifacts are missing after run completion",
    RUN_NEGATIVE: "Completed with a negative result (exit code listed in negative_exit_codes) — not a failure",
    DEP_NEGATIVE: "Cancelled because a dependency ended with a negative result (on_dependency_negative: cancel)",
    STOP_RAM: "Stopped because host RAM exceeded the hard limit",
    STOP_VRAM: "Stopped because GPU VRAM exceeded the hard limit",
    DEP_CANCELLED: "Cancelled because a dependency failed (on_dependency_failure: cancel) — tq retry <root> --downstream revives it",
    STOP_SCAVENGER: "Scavenger task cooperatively suspended to free capacity for other work; resumes when idle",
    STOP_DEPS: (
        "Stopped because a depends_on peer is no longer completed "
        "(upstream retried or otherwise invalidated while this task was running)"
    ),
    INFRA_WORKER_LOST: "Worker lease expired and the worker's own heartbeat is also stale/offline",
    INFRA_CLOCK_JUMP: "Host slept or clock jumped — task interrupted and re-queued",
    INFRA_CLOCK_JUMP_FLAP: (
        "Too many clock-jump interrupts in a short window — left interrupted; use tq retry"
    ),
    INFRA_LEASE_EXPIRED_WORKER_ALIVE: (
        "Run's lease expired but the worker is still heartbeating (reachable, alive) -- "
        "likely a stuck task on an otherwise-healthy worker, not a dead worker"
    ),
    INFRA_HUNG: (
        "Task shows no stdout progress for hung_task_k consecutive ticks while worker is online; "
        "re-queued because idempotent: true"
    ),
    INFRA_ORPHAN: (
        "Local run left running after daemon death / dirty restart — interrupted and re-queued"
    ),
}


def describe(code: str | None) -> str:
    if not code:
        return ""
    return _DESCRIPTIONS.get(code, code)


def all_codes() -> dict[str, str]:
    return dict(_DESCRIPTIONS)


# W6.4: Error category classification
_CATEGORY_OOM = "OOM"
_CATEGORY_TIMEOUT = "Timeout"
_CATEGORY_CRASH = "Crash"
_CATEGORY_CANCELLED = "Cancelled"
_CATEGORY_INFRA = "Infra"

_CODE_TO_CATEGORY: dict[str, str] = {
    RUN_CAP: _CATEGORY_TIMEOUT,
    RUN_FROZEN: _CATEGORY_TIMEOUT,
    RUN_ARTIFACTS_MISSING: _CATEGORY_CRASH,
    STOP_RAM: _CATEGORY_OOM,
    STOP_VRAM: _CATEGORY_OOM,
    STOP_DEPS: _CATEGORY_CANCELLED,
    STOP_SCAVENGER: _CATEGORY_CANCELLED,
    DEP_CANCELLED: _CATEGORY_CANCELLED,
    INFRA_WORKER_LOST: _CATEGORY_INFRA,
    INFRA_CLOCK_JUMP: _CATEGORY_INFRA,
    INFRA_CLOCK_JUMP_FLAP: _CATEGORY_INFRA,
    INFRA_LEASE_EXPIRED_WORKER_ALIVE: _CATEGORY_INFRA,
    INFRA_HUNG: _CATEGORY_INFRA,
    INFRA_ORPHAN: _CATEGORY_INFRA,
    RUN_PREFLIGHT: _CATEGORY_INFRA,
}


_BLOCK_KIND_MAP: dict[str, str] = {
    BLOCK_DEPS: "deps",
    BLOCK_RESOURCES: "resources",
    BLOCK_EXCLUSIVE: "exclusive",
    BLOCK_EXCLUSIVE_PROJECT: "exclusive",
    BLOCK_REQUIRES_PATHS: "resources",
    BLOCK_VRAM_EXCEEDS_SYSTEM: "resources",
    BLOCK_SERVICE: "service",
    BLOCK_SERVICE_VRAM: "service",
    BLOCK_MUTEX: "lock",
    BLOCK_SCAVENGER: "resources",
    BLOCK_MAX_ATTEMPTS: "max_attempts",
    RUN_FROZEN: "frozen",
}


def block_kind(error_code: str | None) -> str | None:
    """Derive a stable block_kind enum from last_error_code.

    Returns one of: 'deps', 'resources', 'exclusive', 'service', 'lock',
    'max_attempts', 'frozen', 'paused' (for a paused project), or None if
    the task is not blocked.
    """
    if not error_code:
        return None
    return _BLOCK_KIND_MAP.get(error_code)


def classify_error(error_code: str | None, exit_code: int | None, stopped_by: str | None) -> str | None:
    """W6.4: Classify a terminal run into a broad error category."""
    if not error_code and (exit_code is None or exit_code == 0):
        return None
    if error_code in _CODE_TO_CATEGORY:
        return _CODE_TO_CATEGORY[error_code]
    if stopped_by in ("user", "tq"):
        return _CATEGORY_CANCELLED
    if stopped_by in ("host-sleep", "lease-timeout"):
        return _CATEGORY_INFRA
    if exit_code is not None and exit_code != 0:
        # OOM heuristic: Linux OOM killer uses 137 (SIGKILL), Windows can also use it
        if exit_code in (137, -9):
            return _CATEGORY_OOM
        return _CATEGORY_CRASH
    return None
