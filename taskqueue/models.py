"""Shared data models for the task queue."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TaskResources:
    cpu_cores: int = 1
    ram_gib: float = 0.0
    gpu_vram_gib: float = 0.0
    locks: tuple[str, ...] = field(default_factory=tuple)  # S2: named mutex names


@dataclass(frozen=True)
class QueueTask:
    project: str
    task_id: str
    description: str
    command: Path
    args: tuple[str, ...]
    resume_args: tuple[str, ...] | None
    wall_clock_cap_sec: int | None
    ledger_subdir: str | None  # opaque passthrough, project-specific meaning
    requires_paths: tuple[Path, ...]
    resources: TaskResources
    priority: int
    exclusive: bool
    enabled: bool
    yaml_source: str
    exclusive_project: bool = False  # solo within project; other projects unaffected
    depends_on: tuple[str, ...] = field(default_factory=tuple)  # AND: all must complete
    depends_on_any: tuple[str, ...] = field(default_factory=tuple)  # OR: any one must complete
    dispatch: str = "local"
    requires: dict[str, Any] = field(default_factory=dict)
    bundle_hash: str | None = None
    code_provider: str = ""  # git_patch | bundle | project_root | "" (auto)
    max_attempts: int = 0  # 0 = unlimited
    notify_url: str | None = None  # opt-in terminal webhook + notify.jsonl
    tags: tuple[str, ...] = field(default_factory=tuple)  # free-form labels for filtering
    suite: str = ""  # logical benchmark/work suite this task belongs to
    env: dict[str, str] = field(default_factory=dict)  # extra env vars to merge into subprocess env
    idempotent: bool = False  # safe to re-run from scratch; enables auto-reassign on hung remote tasks
    # gpt.md R-011: idempotent | checkpointable | non_idempotent | at_most_once
    execution_semantics: str = "non_idempotent"
    metadata: dict[str, Any] = field(default_factory=dict)  # W6.9: opaque passthrough, never interpreted
    dry_run: bool = False  # W6.15: inject TQ_DRY_RUN=1 into subprocess env; advisory to the task
    group: str = ""  # W6.2: logical task group for bulk ops
    artifacts: tuple[str, ...] = field(default_factory=tuple)  # W6.10: paths to verify at terminal state
    artifacts_required: bool = False  # W6.10: fail if any declared artifact is missing
    on_complete: tuple[str, ...] = field(default_factory=tuple)  # task IDs to re-queue on completion
    on_fail: tuple[str, ...] = field(default_factory=tuple)  # task IDs to re-queue on failure/interruption
    transfer: tuple[dict, ...] = field(default_factory=tuple)  # W7.6: file transfer specs [{src, dst, direction}]
    requires_services: tuple[dict, ...] = field(default_factory=tuple)  # R1: [{name, health_url, timeout_sec}]
    overridable_args: tuple[str, ...] = field(default_factory=tuple)  # R2: flags the caller may override per-run
    hung_task_k: int | None = None          # S8: per-task override for global hung_task_k
    schedule: dict | None = None            # S3: {cron: "0 2 * * *"} recurring re-queue
    rearm_on: str = ""                      # S4: "upstream_success" — re-queue when depends_on peer completes
    teardown: dict | None = None            # S5: {command, args, best_effort, timeout_sec}
    # Cap auto-retry (opt-in): re-queue checkpointable/idempotent tasks after wall_clock_cap.
    auto_retry_on_cap: bool = False
    max_auto_retries: int = 20              # 0 = unlimited; default 20
    data: dict | None = None               # D1: data.inputs / outputs / services block
    on_dependency_failure: str = "block"    # T-206: block | cancel
    negative_exit_codes: tuple[int, ...] = ()  # exit codes meaning "ran fine, answer is no" -> completed + tq.run.negative
    on_dependency_negative: str = "run"     # run | cancel when a dependency ended negative
    task_class: str = "normal"              # T-204: normal | scavenger (idle-capacity filler, preemptible)
    min_trust: str = ""                     # C2: minimum worker trust tier: trusted | lab | spot | untrusted | ""

    def argv(self, *, use_resume: bool = False) -> list[str]:
        args = self.resume_args if use_resume and self.resume_args else self.args
        return [str(self.command), *args]


@dataclass
class RunRecord:
    run_id: int
    project: str
    task_id: str
    started_at: str
    finished_at: str | None
    exit_code: int | None
    log_path: str | None
    status: str  # running | completed | failed | interrupted
    progress_pct: float | None
    last_log_line: str | None
    peak_ram_gib: float | None = 0.0
    peak_vram_gib: float | None = 0.0
    worker_id: str | None = None


# Task status values
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_INTERRUPTED = "interrupted"
STATUS_SUSPENDED = "suspended"
STATUS_STOPPED = "stopped"  # host resource eviction (e.g. RAM hard limit); auto-restartable
STATUS_UNKNOWN = "unknown"  # lost attempt; operator must decide (non_idempotent / at_most_once)
STATUS_DISABLED = "disabled"
STATUS_CANCELLED = "cancelled"  # T-206: dependent cancelled because a dependency failed (on_dependency_failure: cancel)

# Cooperative suspend: task exits this code after honoring control-file request.
EXIT_SUSPENDED = 75


def task_key(project: str, task_id: str) -> str:
    """Display / CLI form of a task identity."""
    return f"{project}/{task_id}"


