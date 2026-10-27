"""Task YAML parsing and validation (extracted from db.py; db re-exports these names)."""

from __future__ import annotations

import itertools
import re
import warnings
from dataclasses import replace as _dc_replace
from pathlib import Path

from taskqueue.config import Project
from taskqueue.models import QueueTask, TaskResources

# Top-level YAML keys a task may use (W6.12); anything else is rejected unless it starts with x-.
_KNOWN_YAML_KEYS = frozenset({
    "task_id", "id", "command", "description", "args", "resume_args",
    "priority", "enabled", "exclusive", "exclusive_project", "resources", "requires_paths",
    "requires", "depends_on", "depends_on_any", "dispatch", "wall_clock_cap_sec", "ledger_subdir",
    "bundle", "bundle_hash", "code", "max_attempts", "notify_url", "tags", "suite",
    "env", "idempotent", "execution_semantics", "metadata", "dry_run", "group",
    "artifacts", "artifacts_required",
    "on_complete", "on_fail", "transfer",
    "requires_services", "overridable_args",
    "hung_task_k", "schedule", "rearm_on", "teardown",
    "matrix",
    "auto_retry_on_cap", "max_auto_retries",
    "min_trust", "data", "class", "on_dependency_failure",
    "negative_exit_codes", "on_dependency_negative",
})

# Device names Windows refuses as file names (task ids become log/control file names).
_WIN_RESERVED = frozenset({
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
})

_KNOWN_SERVICES: dict[str, dict] = {
    "ollama": {"name": "ollama", "health_url": "http://localhost:11434/api/version", "timeout_sec": 5},
    "docker": {"name": "docker", "health_url": "http://localhost:2375/version", "timeout_sec": 5},
    "comfyui": {"name": "comfyui", "health_url": "http://localhost:8188/system_stats", "timeout_sec": 5},
    "stable-diffusion-webui": {"name": "stable-diffusion-webui", "health_url": "http://localhost:7860/internal/sysinfo", "timeout_sec": 5},
}


def _expand_service(s: object) -> dict:
    """Expand a string shorthand (e.g. 'ollama') to a full requires_services dict."""
    if isinstance(s, str):
        return _KNOWN_SERVICES.get(s, {"name": s})
    return dict(s)  # type: ignore[arg-type]


def _relativize_path(p_str: str, project: "Project") -> Path:
    """Store requires_paths relative to project root (POSIX separators)."""
    try:
        abs_p = project.resolve(p_str)
        return Path(abs_p.relative_to(project.root.resolve()).as_posix())
    except ValueError:
        return Path(str(p_str).replace("\\", "/"))


def validate_task_spec(raw: object, path: Path) -> None:
    """Reject malformed task mappings with a clear ValueError (KeyError for a missing id).

    Shared by the YAML parser; also usable for a dry-run validate endpoint.
    """
    name = getattr(path, "name", str(path))
    if not isinstance(raw, dict) or not raw:
        raise ValueError(f"{name}: task YAML is empty or not a mapping")
    raw_id = raw.get("task_id") or raw.get("id")
    if raw_id is None:
        raise KeyError("YAML must have 'task_id' (or 'id') field")
    tid = str(raw_id)
    if "\\" in tid or ".." in tid or any(ord(c) < 32 for c in tid):
        raise ValueError(f"{name}: task_id {tid!r} must not contain backslash, '..' or control characters")
    cmd = raw.get("command")
    if cmd is None or not str(cmd).strip():
        raise ValueError(f"{name}: 'command' is required and must not be empty")
    for key in ("wall_clock_cap_sec", "max_attempts"):
        val = raw.get(key)
        if val is None:
            continue
        try:
            ok = int(val) >= 0
        except (TypeError, ValueError):
            ok = False
        if not ok:
            raise ValueError(f"{name}: {key!r} must be a non-negative integer, got {val!r}")
    _prio = raw.get("priority")
    if _prio is not None:
        try:
            int(_prio)
        except (TypeError, ValueError):
            raise ValueError(f"{name}: 'priority' must be an integer, got {_prio!r}") from None
    _xfer = raw.get("transfer")
    if _xfer is not None:
        if not isinstance(_xfer, list) or not all(isinstance(e, dict) for e in _xfer):
            raise ValueError(f"{name}: 'transfer' must be a list of mappings")
    _sched = raw.get("schedule")
    if isinstance(_sched, dict) and _sched.get("cron"):
        from taskqueue.cron import cron_valid

        if not cron_valid(_sched["cron"]):
            raise ValueError(
                f"{name}: schedule.cron {_sched['cron']!r} is not a valid 5-field cron expression"
            )
    res = raw.get("resources")
    if isinstance(res, dict):
        for key in ("cpu_cores", "ram_gib", "gpu_vram_gib"):
            val = res.get(key)
            if val is None:
                continue
            try:
                ok = float(val) >= 0
            except (TypeError, ValueError):
                ok = False
            if not ok:
                raise ValueError(f"{name}: resources.{key} must be a non-negative number, got {val!r}")


def _parse_resources(raw: dict, path: Path) -> TaskResources:
    """``resources:`` mapping -> TaskResources (cpu rounded up)."""
    from taskqueue.rows import resources_dict_from_mapping

    res_raw = raw.get("resources", {})
    if not isinstance(res_raw, dict):
        raise ValueError(f"{path.name}: 'resources' must be a mapping, got {type(res_raw).__name__}")
    norm = resources_dict_from_mapping(res_raw)
    return TaskResources(
        cpu_cores=int(norm["cpu_cores"]),
        ram_gib=float(norm["ram_gib"]),
        gpu_vram_gib=float(norm["gpu_vram_gib"]),
        locks=tuple(norm["locks"]),
    )


def _validated_task_id(raw: dict, path: Path) -> str:
    """task_id (or ``id``) with filesystem / Windows-reserved-name checks."""
    raw_id = raw.get("task_id") or raw.get("id")  # presence checked in validate_task_spec
    if "/" in str(raw_id):
        raise ValueError(f"task_id must not contain '/': {raw_id!r}")
    _stem = str(raw_id).split(".", 1)[0].rstrip(" .").upper()
    if str(raw_id).endswith((" ", ".")):
        raise ValueError(f"{path.name}: task_id {raw_id!r} must not end with a space or dot")
    if _stem in _WIN_RESERVED:
        raise ValueError(f"{path.name}: task_id {raw_id!r} is a reserved name on Windows")
    return str(raw_id)


def _parse_command(raw: dict, path: Path, project: "Project") -> Path:
    """``command:`` -> resolved path (or bare name for PATH lookup)."""
    raw_cmd = str(raw["command"])
    # A2: Validate command uses forward slashes (only for relative/bare paths, not absolute)
    if raw_cmd and "\\" in raw_cmd and not Path(raw_cmd).is_absolute():
        raise ValueError(
            f"{path.name}: 'command' must use forward slashes (/), not backslashes: {raw_cmd!r}"
        )
    # A2: Warn about Windows absolute paths in args
    for _i, _arg in enumerate(raw.get("args") or []):
        if len(str(_arg)) > 2 and str(_arg)[1] == ":" and str(_arg)[0].isalpha():
            warnings.warn(
                f"{path.name}: arg[{_i}] looks like a Windows absolute path: {_arg!r}. "
                "Use project-relative paths for cross-platform compatibility."
            )
    # Absolute path or contains a separator -> resolve vs project root.
    # Bare command name (no separators) -> keep bare so shutil.which resolves
    # it at preflight (e.g. "wsl", "python").
    if Path(raw_cmd).is_absolute() or "/" in raw_cmd or "\\" in raw_cmd:
        cmd_path = project.resolve(raw_cmd)
    else:
        cmd_path = Path(raw_cmd)
    return cmd_path


def _parse_semantics(raw: dict, path: Path) -> tuple[str, bool]:
    """(execution_semantics, idempotent) with the idempotent column kept in sync."""
    from taskqueue.fencing import resolve_execution_semantics, semantics_implies_idempotent

    idempotent_flag = bool(raw.get("idempotent", False))
    try:
        execution_semantics = resolve_execution_semantics(
            raw.get("execution_semantics"),
            idempotent=idempotent_flag,
        )
    except ValueError as e:
        raise ValueError(f"{path.name}: {e}") from e
    # Keep idempotent column in sync for victim selection / hung paths.
    idempotent_flag = semantics_implies_idempotent(execution_semantics)
    return execution_semantics, idempotent_flag


def _parse_dependency_policy(raw: dict, path: Path) -> tuple[str, tuple, str]:
    """(on_dependency_failure, negative_exit_codes, on_dependency_negative)."""
    # T-206: what to do when a dependency dies
    on_dep_failure = str(raw.get("on_dependency_failure") or "block").strip().lower()
    if on_dep_failure not in ("block", "cancel"):
        raise ValueError(
            f"{path.name}: on_dependency_failure must be 'block' or 'cancel', got {on_dep_failure!r}"
        )
    # Negative results: exit codes that mean "ran fine, answer is no".
    _neg_raw = raw.get("negative_exit_codes") or []
    if not isinstance(_neg_raw, list) or not all(isinstance(c, int) and not isinstance(c, bool) for c in _neg_raw):
        raise ValueError(f"{path.name}: negative_exit_codes must be a list of integers")
    if 0 in _neg_raw:
        raise ValueError(f"{path.name}: negative_exit_codes cannot contain 0")
    negative_exit_codes = tuple(sorted(set(_neg_raw)))
    on_dep_negative = str(raw.get("on_dependency_negative") or "run").strip().lower()
    if on_dep_negative not in ("run", "cancel"):
        raise ValueError(f"{path.name}: on_dependency_negative must be 'run' or 'cancel', got {on_dep_negative!r}")
    return on_dep_failure, negative_exit_codes, on_dep_negative


def _parse_class_and_trust(raw: dict, path: Path, execution_semantics: str) -> tuple[str, str]:
    """(task_class, min_trust) with the scavenger/at_most_once conflict check."""
    # T-204: scheduling class
    task_class = str(raw.get("class") or "normal").strip().lower()
    if task_class not in ("normal", "scavenger"):
        raise ValueError(
            f"{path.name}: class must be 'normal' or 'scavenger', got {task_class!r}"
        )
    if task_class == "scavenger" and execution_semantics == "at_most_once":
        raise ValueError(
            f"{path.name}: class: scavenger is preempted and restarted, which "
            "contradicts execution_semantics: at_most_once"
        )
    # C2: min_trust — minimum worker trust tier required
    min_trust = str(raw.get("min_trust") or "").strip().lower()
    _VALID_TRUST = {"", "trusted", "lab", "spot", "untrusted"}
    if min_trust not in _VALID_TRUST:
        raise ValueError(
            f"{path.name}: min_trust must be one of {sorted(_VALID_TRUST)!r}, got {min_trust!r}"
        )
    return task_class, min_trust


def _parse_raw_task_dict(
    raw: dict | None,
    path: Path,
    project: "Project",
    *,
    allow_remote_notify: bool = False,
) -> "QueueTask":
    """Parse a single already-loaded YAML mapping into a QueueTask."""
    from taskqueue.notify import validate_notify_url

    validate_task_spec(raw, path)

    # W6.12: Reject unknown top-level YAML keys; x-* and metadata: are escape hatches.
    _unknown = [k for k in (raw or {}) if k not in _KNOWN_YAML_KEYS and not str(k).startswith("x-")]
    if _unknown:
        from taskqueue.yaml_legacy import suggest_unknown_key

        suggestions = [suggest_unknown_key(str(uk), _KNOWN_YAML_KEYS) for uk in _unknown]
        raise ValueError(
            f"{path.name}: unknown YAML key(s):\n" + "\n".join(suggestions)
        )

    _rs_raw = raw.get("requires_services")
    if _rs_raw is not None and not isinstance(_rs_raw, list):
        raise ValueError(
            f"{path.name}: 'requires_services' must be a list, got {type(_rs_raw).__name__}"
        )
    resources = _parse_resources(raw, path)
    raw_requires_paths = raw.get("requires_paths", []) or []
    # A2: Validate requires_paths use forward slashes
    for _rp in raw_requires_paths:
        if "\\" in str(_rp):
            raise ValueError(
                f"{path.name}: 'requires_paths' entries must use forward slashes: {_rp!r}"
            )
    raw_args = raw.get("args", []) or []
    raw_resume = raw.get("resume_args")

    raw_id = _validated_task_id(raw, path)

    cmd_path = _parse_command(raw, path, project)

    raw_depends = raw.get("depends_on") or []
    raw_depends_any = raw.get("depends_on_any") or []
    # Fleet projects (remote: true): omitted dispatch → any so workers can claim.
    # Explicit dispatch: local still pins the task to the master.
    if "dispatch" in raw:
        raw_dispatch = str(raw.get("dispatch") or "local")
    elif getattr(project, "remote", False):
        raw_dispatch = "any"
    else:
        raw_dispatch = "local"
    raw_requires_constraints = raw.get("requires") or {}
    if not isinstance(raw_requires_constraints, dict):
        raw_requires_constraints = {}

    raw_bundle_hash = raw.get("bundle", {}).get("hash") if isinstance(raw.get("bundle"), dict) else raw.get("bundle_hash")
    if raw_bundle_hash is not None:
        raw_bundle_hash = str(raw_bundle_hash)

    raw_code = raw.get("code") if isinstance(raw.get("code"), dict) else {}
    raw_code_provider = str(raw_code.get("provider") or "").strip().lower()
    if raw_code_provider:
        from taskqueue.provision import VALID_CODE_PROVIDERS
        if raw_code_provider not in VALID_CODE_PROVIDERS:
            raise ValueError(
                f"{path.name}: code.provider must be one of "
                f"{sorted(VALID_CODE_PROVIDERS)}, got {raw_code_provider!r}"
            )

    max_attempts = int(raw.get("max_attempts") or 0)

    auto_retry_on_cap = bool(raw.get("auto_retry_on_cap", False))
    if raw.get("max_auto_retries") is not None:
        max_auto_retries = int(raw["max_auto_retries"])
        if max_auto_retries < 0:
            raise ValueError(f"{path.name}: max_auto_retries must be >= 0")
    else:
        max_auto_retries = 20

    notify_url = None
    raw_notify = raw.get("notify_url")
    if raw_notify is not None and str(raw_notify).strip():
        if not isinstance(raw_notify, str):
            raise ValueError("notify_url must be a string")
        notify_url = validate_notify_url(
            raw_notify, allow_remote=bool(allow_remote_notify)
        )

    raw_tags = raw.get("tags") or []
    if isinstance(raw_tags, str):
        raw_tags = [raw_tags]

    env = {str(k): str(v) for k, v in (raw.get("env") or {}).items()}
    if bool(raw.get("dry_run", False)):
        env.setdefault("TQ_DRY_RUN", "1")

    execution_semantics, idempotent_flag = _parse_semantics(raw, path)

    # A2: Validate transfer src/dst use forward slashes
    for _t in (raw.get("transfer") or []):
        for _tf in ("src", "dst"):
            if _t.get(_tf) and "\\" in str(_t[_tf]):
                raise ValueError(
                    f"{path.name}: transfer.{_tf} must use forward slashes: {_t[_tf]!r}"
                )

    raw_data = raw.get("data")
    if raw_data is not None and not isinstance(raw_data, dict):
        raise ValueError(f"{path.name}: 'data' must be a mapping")

    on_dep_failure, negative_exit_codes, on_dep_negative = _parse_dependency_policy(raw, path)
    task_class, min_trust = _parse_class_and_trust(raw, path, execution_semantics)

    return QueueTask(
        project=project.name,
        task_id=str(raw_id),
        description=str(raw.get("description", "")),
        command=cmd_path,
        args=tuple(str(a) for a in raw_args),
        resume_args=tuple(str(a) for a in raw_resume) if raw_resume else None,
        wall_clock_cap_sec=int(raw["wall_clock_cap_sec"]) if raw.get("wall_clock_cap_sec") is not None else None,
        ledger_subdir=raw.get("ledger_subdir"),
        requires_paths=tuple(_relativize_path(str(p), project) for p in raw_requires_paths),
        resources=resources,
        priority=max(0, min(100, int(50 if raw.get("priority") is None else raw["priority"]))),
        exclusive=bool(raw.get("exclusive", False)),
        exclusive_project=bool(raw.get("exclusive_project", False)),
        enabled=bool(raw.get("enabled", True)),
        yaml_source=str(path),
        depends_on=tuple(str(d) for d in raw_depends),
        depends_on_any=tuple(str(d) for d in raw_depends_any),
        dispatch=raw_dispatch,
        requires=raw_requires_constraints,
        bundle_hash=raw_bundle_hash,
        code_provider=raw_code_provider,
        max_attempts=max_attempts,
        notify_url=notify_url,
        tags=tuple(str(t) for t in raw_tags),
        suite=str(raw.get("suite", "") or ""),
        env=env,
        idempotent=idempotent_flag,
        execution_semantics=execution_semantics,
        metadata=dict(raw.get("metadata") or {}),
        dry_run=bool(raw.get("dry_run", False)),
        group=str(raw.get("group") or ""),
        artifacts=tuple(str(a) for a in (raw.get("artifacts") or [])),
        artifacts_required=bool(raw.get("artifacts_required", False)),
        on_complete=tuple(str(t) for t in (raw.get("on_complete") or [])),
        on_fail=tuple(str(t) for t in (raw.get("on_fail") or [])),
        transfer=tuple(dict(e) for e in (raw.get("transfer") or [])),
        requires_services=tuple(_expand_service(s) for s in (raw.get("requires_services") or [])),
        overridable_args=tuple(str(a) for a in (raw.get("overridable_args") or [])),
        hung_task_k=int(raw["hung_task_k"]) if raw.get("hung_task_k") is not None else None,
        schedule=dict(raw["schedule"]) if isinstance(raw.get("schedule"), dict) else None,
        rearm_on=str(raw.get("rearm_on") or ""),
        teardown=dict(raw["teardown"]) if isinstance(raw.get("teardown"), dict) else None,
        auto_retry_on_cap=auto_retry_on_cap,
        max_auto_retries=max_auto_retries,
        data=raw_data,
        min_trust=min_trust,
        task_class=task_class,
        on_dependency_failure=on_dep_failure,
        negative_exit_codes=negative_exit_codes,
        on_dependency_negative=on_dep_negative,
    )


def parse_task_yaml(
    path: Path,
    project: "Project",
    *,
    allow_remote_notify: bool = False,
) -> "QueueTask":
    import yaml

    docs = _load_yaml_docs(path.read_text(encoding="utf-8"))
    if len(docs) > 1:
        raise ValueError(
            f"{path.name}: contains {len(docs)} YAML documents separated by '---'; "
            "use parse_task_yaml_many() to load all tasks from a multi-document file"
        )
    return _parse_raw_task_dict(docs[0] if docs else None, path, project,
                                allow_remote_notify=allow_remote_notify)


def _load_yaml_docs(text: str) -> list:
    """``yaml.safe_load_all`` through libyaml's C loader when present (several times faster)."""
    import yaml

    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    return list(yaml.load_all(text, Loader=loader))  # noqa: S506 - SafeLoader variant


def parse_task_yaml_many(
    path: Path,
    project: Project,
    *,
    allow_remote_notify: bool = False,
) -> list[QueueTask]:
    """R10: Parse a YAML file, expanding `matrix:` or multi-document format into multiple QueueTask objects.

    Without `matrix:` and without `---` separators, returns a single-element list.
    With `matrix: {key: [v1, v2, ...]}`, returns one task per combination.
    With `---`-separated documents, returns one task per document (multi-document format).
    Task IDs become `{base_id}_{key}{value}` (single axis) or `{base_id}_{idx}` (multi-axis).
    Each matrix value is appended to `args` as `--{key}={value}` and stored in `metadata`.
    """
    import yaml

    docs = _load_yaml_docs(path.read_text(encoding="utf-8"))

    # Multi-document YAML: each --- document is an independent task.
    if len(docs) > 1:
        return [
            _parse_raw_task_dict(doc, path, project, allow_remote_notify=allow_remote_notify)
            for doc in docs
            if doc
        ]

    raw = docs[0] if docs else None
    matrix = raw.get("matrix") if raw else None
    if not matrix or not isinstance(matrix, dict):
        return [_parse_raw_task_dict(raw, path, project, allow_remote_notify=allow_remote_notify)]

    base = _parse_raw_task_dict(raw, path, project, allow_remote_notify=allow_remote_notify)
    axes = [(k, list(v) if isinstance(v, (list, tuple)) else [v]) for k, v in matrix.items()]
    keys = [k for k, _ in axes]
    combos = list(itertools.product(*[vals for _, vals in axes]))

    tasks: list[QueueTask] = []
    def _safe_matrix_part(val: object) -> str:
        s = str(val)
        if re.search(r'[/\s\\]', s):
            raise ValueError(
                f"matrix value {s!r} contains '/', backslash, or whitespace — "
                "invalid in a task_id"
            )
        return s

    for idx, combo in enumerate(combos):
        suffix = f"_{keys[0]}{_safe_matrix_part(combo[0])}" if len(keys) == 1 else f"_{idx}"
        extra_args = tuple(f"--{k}={v}" for k, v in zip(keys, combo))
        merged_meta = dict(base.metadata)
        for k, v in zip(keys, combo):
            merged_meta[k] = v
        tasks.append(_dc_replace(
            base,
            task_id=f"{base.task_id}{suffix}",
            args=base.args + extra_args,
            metadata=merged_meta,
        ))
    return tasks
