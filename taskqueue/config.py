"""State home resolution and project registry.

All daemon state lives in one directory (default ``~/.taskqueue``, override
with the ``TASKQUEUE_HOME`` environment variable):

    ~/.taskqueue/
      projects.yaml     # project registry (see Project)
      queue.db          # one SQLite DB, all projects
      daemon.pid
      daemon.log
      logs/<project>/<task_id>_<ts>.log

``projects.yaml`` format::

    projects:
      pokerbot:
        root: D:\\antigravity\\pokerbot
        queue_dir: experiments/operator_queue     # optional, this is the default
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

DEFAULT_QUEUE_DIR = "experiments/operator_queue"
ENV_HOME = "TASKQUEUE_HOME"

# Master↔worker / client control protocol major version (bump on breaking changes).
PROTOCOL_VERSION = 1
MIN_SUPPORTED_PROTOCOL = 1
MAX_SUPPORTED_PROTOCOL = PROTOCOL_VERSION

# Remote run lease TTL (seconds). Heartbeat / log-chunk renews the lease.
LEASE_TTL_SEC = 90
# Worker stops the child this many seconds before lease expiry if heartbeats fail (R-012).
LEASE_SAFETY_MARGIN_SEC = 15
# Workers with last_seen older than this are marked offline.
WORKER_OFFLINE_SEC = 90
# Heartbeat interval (seconds). Halved from 30s to 15s alongside G1 command long-poll.
WORKER_HEARTBEAT_SEC = 15
# R-007: max seconds a worker may long-poll GET /api/worker/next-task?wait=
WORK_LONGPOLL_MAX_SEC = 30
# F-074: master fencing lease (daemon incarnation). Longer than run leases so a
# blocked scheduler tick / heavy sync cannot expire fencing within ~90s and 503
# all worker register/heartbeat/next-task calls ("not master").
MASTER_LEASE_TTL_SEC = 600
MASTER_LEASE_RENEW_SEC = 60

VALID_CONFIG_KEYS = {
    "cpu_limit_pct": (int, 10, 100),
    "gpu_limit_pct": (int, 0, 100),
    "ram_limit_pct": (int, 10, 100),
    "ram_hard_limit_pct": (int, 10, 100),
    "graceful_stop_sec": (int, 5, 300),
    "scheduler_interval_sec": (int, 1, 60),
    "heartbeat_sec": (int, 5, 300),
    "frozen_threshold_sec": (int, 60, 3600),
    "web_port": (int, 1024, 65535),
    # Slice H publish
    "bind_host": (str, 1, 253),       # length bounds
    "auth_token": (str, 0, 256),
    "prevent_sleep": (int, 0, 1),
    "suspend_grace_sec": (int, 0, 600),
    "notify_allow_remote": (int, 0, 1),
    "max_running_tasks": (int, 0, 256),
    "gpu_vram_limit_gib": (float, 0.0, 1000.0),
    "max_cpu_cores": (int, 0, 1024),
    "max_ram_gib": (float, 0.0, 10000.0),
    "notify_on_attention": (int, 0, 1),
    "toast_notifications": (int, 0, 1),
    "max_queued_tasks_per_project": (int, 0, 10000),
    # F-046: these were in _DEFAULT_CONFIG but missing here — couldn't be set via `tq config set`
    "fairshare_halflife_days": (float, 0.1, 365.0),
    "fairshare_weight": (float, 0.0, 1.0),
    "hung_task_k": (int, 0, 100),
    # scheduler_resources.md — admission control
    "default_task_ram_gib": (float, 0.0, 1000.0),
    "resource_predict_runs": (int, 1, 100),
    "admit_max_per_tick": (int, 0, 256),
    "admit_candidate_window": (int, 0, 10000),
    "admit_settle_sec": (float, 0.0, 60.0),
    "max_concurrent_unknown": (int, 0, 256),
    "settle_window_sec": (int, 0, 3600),
    "stop_max_per_tick": (int, 1, 32),
    "recovery_cooldown_sec": (int, 0, 3600),
    "scavenger_preempt_cooldown_sec": (int, 0, 3600),
    "admission_shadow_mode": (int, 0, 1),
    # P6-P8
    "reservation_max_ticks": (int, 0, 3600),
    "cpu_measure_ema_sec": (float, 1.0, 600.0),
    "vram_hard_limit_pct": (int, 0, 100),
    # F-001: 0 = auto (max(2*scheduler_interval, 30)); >0 = explicit seconds
    "clock_jump_threshold_sec": (int, 0, 86400),
    "clock_jump_max_requeue": (int, 0, 1000),
    "clock_jump_requeue_window_sec": (int, 60, 86400),
    # F-075: documented tunables that cfg.get reads but `tq config set` rejected
    "gpu_util_warn_pct": (float, 0.0, 100.0),
    "ram_settle_sec": (float, 0.0, 86400.0),
    "ram_leak_warn_gib_per_hour": (float, 0.0, 1000.0),
    "max_concurrent_tasks": (int, 0, 256),
    # PC-1: remote best-fit placement (plan/placement_capacity.md)
    "placement_mode": (str, 1, 32),
    "placement_age_sec": (int, 0, 86400),
    "placement_shadow_mode": (int, 0, 1),
    "placement_prefer_last_worker": (int, 0, 1),
    # String enums used by the web config modal (saveCfg POSTs these with
    # the numeric globals; rejecting them 400s the whole /api/config body).
    "limit_decrease_policy": (str, 1, 32),
    "frozen_action": (str, 1, 32),
    "notify_url": (str, 0, 2048),
    "notify_default": (int, 0, 1),  # 1 (default): every terminal task writes notify.jsonl
    # ds2 §7.28: retention keys were missing — `tq config set retention_enabled true` failed
    "retention_enabled": (int, 0, 1),
    "retention_log_days": (int, 0, 36500),
    "retention_run_days": (int, 0, 36500),
    "retention_projects": (str, 0, 4096),
    # 0 = off; when set, finished task logs larger than this are truncated to a byte tail.
    "retention_log_max_bytes": (int, 0, 64 * 1024 * 1024),
    # Aggregated agent/API usage counters (api_usage); see docs/security.md.
    "usage_tracking": (int, 0, 1),
    "retention_usage_days": (int, 1, 36500),
    "retention_audit_days": (int, 1, 36500),
}

_ENUM_CONFIG: dict[str, frozenset[str]] = {
    "limit_decrease_policy": frozenset({"ignore", "evict", "block"}),
    "frozen_action": frozenset({"log", "stop", "restart"}),
    "placement_mode": frozenset({"binpack", "first_fit"}),
}


def validate_config(key: str, val: any) -> str | None:
    """Return error message if invalid, else None."""
    if key not in VALID_CONFIG_KEYS:
        return f"Unknown config key '{key}'"
    expected_type, min_val, max_val = VALID_CONFIG_KEYS[key]
    try:
        if expected_type is int:
            parsed = int(val)
        elif expected_type is float:
            parsed = float(val)
        else:
            parsed = str(val)
    except Exception:
        return f"Invalid value '{val}' for '{key}' (expected {expected_type.__name__})"
    if expected_type in (int, float):
        if parsed < min_val or parsed > max_val:
            return f"Value {parsed} for '{key}' must be between {min_val} and {max_val}"
    else:
        if len(parsed) < min_val or len(parsed) > max_val:
            return f"Length of '{key}' must be between {min_val} and {max_val}"
        if key == "bind_host" and not parsed.strip():
            return "bind_host must be non-empty (e.g. 127.0.0.1 or 0.0.0.0)"
    allowed = _ENUM_CONFIG.get(key)
    if allowed is not None and parsed not in allowed:
        return (
            f"Value {parsed!r} for '{key}' must be one of "
            f"{', '.join(sorted(allowed))}"
        )
    return None


def state_home() -> Path:
    env = os.environ.get(ENV_HOME)
    return Path(env) if env else Path.home() / ".taskqueue"


def db_path(home: Path) -> Path:
    return home / "queue.db"


def pid_path(home: Path) -> Path:
    return home / "daemon.pid"


def clean_shutdown_path(home: Path) -> Path:
    """Marker written only on clean daemon exit; consumed on next boot."""
    return home / "daemon.clean"


def daemon_log_path(home: Path) -> Path:
    return home / "daemon.log"


def logs_dir(home: Path, project: str) -> Path:
    return home / "logs" / project


def controls_dir(home: Path) -> Path:
    return home / "controls"


def control_path(home: Path, project: str, task_id: str) -> Path:
    """Per-run cooperative control file (suspend request, etc.)."""
    # project/task_id can come from URLs: never let separators or ".." leave controls/.
    safe = re.sub(r"[\\/:]", "_", f"{project}__{task_id}").replace("..", "__") + ".json"
    return controls_dir(home) / safe


def registry_path(home: Path) -> Path:
    return home / "projects.yaml"


@dataclass(frozen=True)
class Project:
    name: str
    root: Path
    queue_dir: Path  # absolute
    git_url: str | None = None
    remote: bool = False
    """When True, workers may claim tasks and allowlisted file sync is enabled."""
    sync_push: tuple[str, ...] = ()
    """Allowlisted globs (relative to root) for master→worker file sync."""

    def resolve(self, rel: str | Path) -> Path:
        """Resolve a path from a task YAML: absolute stays, relative is vs root."""
        p = Path(rel)
        return p if p.is_absolute() else self.root / p


def project_config_path(root: Path) -> Path:
    """Committed per-project taskqueue config: ``<root>/.taskqueue/project.yaml``."""
    return root / ".taskqueue" / "project.yaml"


def load_project_file_config(root: Path) -> dict:
    """Load ``.taskqueue/project.yaml`` if present. Returns empty dict if missing."""
    import yaml

    path = project_config_path(root)
    if not path.is_file():
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def _parse_sync_push(file_cfg: dict, registry_cfg: dict) -> tuple[str, ...]:
    """Prefer registry ``sync.push`` override; else project file ``sync.push``."""
    for src in (registry_cfg, file_cfg):
        sync = src.get("sync") if isinstance(src, dict) else None
        if not isinstance(sync, dict):
            continue
        push = sync.get("push")
        if push is None:
            continue
        if isinstance(push, str):
            return (push,)
        if isinstance(push, (list, tuple)):
            return tuple(str(p) for p in push if str(p).strip())
    return ()


def load_projects(home: Path) -> dict[str, Project]:
    """Load the registry. Missing file -> empty registry (daemon idles).

    Unknown keys (e.g. legacy ``plan_files``) are ignored.
    Loads sync allowlists from ``<root>/.taskqueue/project.yaml`` when present.
    """
    import yaml

    path = registry_path(home)
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    projects: dict[str, Project] = {}
    for name, cfg in (raw.get("projects") or {}).items():
        cfg = cfg or {}
        if "root" not in cfg:
            raise ValueError(f"projects.yaml: project '{name}' has no 'root'")
        root = Path(str(cfg["root"]))
        queue_rel = str(cfg.get("queue_dir", DEFAULT_QUEUE_DIR))
        queue_dir = Path(queue_rel) if Path(queue_rel).is_absolute() else root / queue_rel
        git_url = cfg.get("git_url")
        if git_url is not None:
            git_url = str(git_url)
        remote = bool(cfg.get("remote", False))
        file_cfg = load_project_file_config(root)
        sync_push = _parse_sync_push(file_cfg, cfg)
        projects[str(name)] = Project(
            name=str(name),
            root=root,
            queue_dir=queue_dir,
            git_url=git_url,
            remote=remote,
            sync_push=sync_push,
        )
    return projects
