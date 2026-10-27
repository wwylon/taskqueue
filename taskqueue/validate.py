"""Validate operator_queue YAML files for registered projects."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from taskqueue.config import load_projects, db_path
from taskqueue.yaml_sync import iter_queue_files
from taskqueue.db import get_config, open_db, parse_task_yaml


def _get_historical_median_sec(conn, project: str, task_id: str) -> float | None:
    """W6.19: Return median wall-clock duration of completed runs for this task."""
    rows = conn.execute(
        "SELECT (julianday(finished_at) - julianday(started_at)) * 86400.0 AS dur "
        "FROM runs WHERE project = ? AND task_id = ? AND status = 'completed' "
        "AND finished_at IS NOT NULL AND started_at IS NOT NULL "
        "ORDER BY run_id DESC LIMIT 20",
        (project, task_id),
    ).fetchall()
    if not rows:
        return None
    durations = sorted(r["dur"] for r in rows if r["dur"] and r["dur"] > 0)
    if not durations:
        return None
    mid = len(durations) // 2
    if len(durations) % 2 == 0:
        return (durations[mid - 1] + durations[mid]) / 2.0
    return durations[mid]


def _project_for_path(yaml_path: Path, home: Path):
    """Resolve registered project owning yaml_path, or a synthetic Project."""
    from taskqueue.config import Project

    projects = load_projects(home)
    for name, p in projects.items():
        try:
            yaml_path.relative_to(p.queue_dir)
            return name, p
        except ValueError:
            pass
    return "__unknown__", Project(
        name="__unknown__",
        root=yaml_path.parent.parent,
        queue_dir=yaml_path.parent,
    )


def _allow_remote_notify(home: Path):
    allow_remote = False
    conn = None
    db_file = db_path(home)
    if db_file.is_file():
        conn = open_db(db_file)
        try:
            allow_remote = bool(get_config(conn).get("notify_allow_remote"))
        except Exception:
            pass
    return allow_remote, conn


def _cap_warnings(conn, project_name: str, task, yaml_path: Path, *, cap_warn_multiplier: float):
    warnings: list[dict[str, str]] = []
    if not (conn and task.wall_clock_cap_sec):
        return warnings
    try:
        median = _get_historical_median_sec(conn, project_name, task.task_id)
        if median and task.wall_clock_cap_sec > cap_warn_multiplier * median:
            warnings.append({
                "project": project_name,
                "task_id": task.task_id,
                "path": str(yaml_path),
                "warning": (
                    f"wall_clock_cap_sec={task.wall_clock_cap_sec} is "
                    f"{task.wall_clock_cap_sec / median:.1f}× the historical median "
                    f"({median:.0f}s) — consider reducing"
                ),
            })
    except Exception:
        pass
    return warnings


def fix_file(yaml_path: Path, home: Path) -> dict[str, Any]:
    """Rewrite known legacy keys in one YAML. May drop comments (PyYAML dump)."""
    from taskqueue.yaml_legacy import fix_legacy_yaml_file

    project_name, _project = _project_for_path(yaml_path, home)
    try:
        changes = fix_legacy_yaml_file(yaml_path, project_name=project_name)
        return {
            "project": project_name,
            "path": str(yaml_path),
            "changes": changes,
            "error": None,
        }
    except Exception as exc:
        return {
            "project": project_name,
            "path": str(yaml_path),
            "changes": [],
            "error": str(exc),
        }


def fix_home(home: Path) -> dict[str, Any]:
    """Rewrite legacy keys in all registered ``*.yaml`` queue files."""
    projects = load_projects(home)
    fixed: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for name, project in projects.items():
        if not project.queue_dir.is_dir():
            continue
        for yaml_path in iter_queue_files(project.queue_dir):
            try:
                from taskqueue.yaml_legacy import fix_legacy_yaml_file

                changes = fix_legacy_yaml_file(yaml_path, project_name=name)
                if changes:
                    fixed.append({
                        "project": name,
                        "path": str(yaml_path),
                        "changes": changes,
                    })
            except Exception as exc:
                errors.append({
                    "project": name,
                    "path": str(yaml_path),
                    "error": str(exc),
                })
    return {"fixed": fixed, "errors": errors, "files_rewritten": len(fixed)}


def validate_file(
    yaml_path: Path,
    home: Path,
    *,
    cap_warn_multiplier: float = 3.0,
    fix: bool = False,
) -> dict[str, Any]:
    """Validate a single YAML file. Ext.1: tq validate <path>."""
    fixed_info: list[dict[str, Any]] = []
    if fix:
        result = fix_file(yaml_path, home)
        if result.get("error"):
            return {
                "projects": 1,
                "ok": 0,
                "errors": [{
                    "project": result["project"],
                    "path": result["path"],
                    "error": result["error"],
                }],
                "warnings": [],
                "fixed": [],
                "valid": False,
            }
        if result.get("changes"):
            fixed_info.append(result)

    project_name, project = _project_for_path(yaml_path, home)
    allow_remote, conn = _allow_remote_notify(home)

    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    ok_count = 0
    try:
        task = parse_task_yaml(yaml_path, project, allow_remote_notify=allow_remote)
        ok_count += 1
        warnings.extend(
            _cap_warnings(
                conn, project_name, task, yaml_path,
                cap_warn_multiplier=cap_warn_multiplier,
            )
        )
    except Exception as exc:
        errors.append({
            "project": project_name,
            "path": str(yaml_path),
            "error": str(exc),
        })
    if conn:
        conn.close()
    return {
        "projects": 1,
        "ok": ok_count,
        "errors": errors,
        "warnings": warnings,
        "fixed": fixed_info,
        "valid": not errors,
    }


def validate_home(
    home: Path,
    *,
    cap_warn_multiplier: float = 3.0,
    fix: bool = False,
) -> dict[str, Any]:
    """Validate YAML files. W6.19: warn when wall_clock_cap_sec > N× historical median."""
    fixed_info: list[dict[str, Any]] = []
    fix_errors: list[dict[str, str]] = []
    if fix:
        fr = fix_home(home)
        fixed_info = fr["fixed"]
        fix_errors = fr["errors"]

    projects = load_projects(home)
    allow_remote, conn = _allow_remote_notify(home)
    errors: list[dict[str, str]] = list(fix_errors)
    warnings: list[dict[str, str]] = []
    ok_count = 0
    for name, project in projects.items():
        if not project.queue_dir.is_dir():
            errors.append({
                "project": name,
                "path": str(project.queue_dir),
                "error": "queue_dir missing",
            })
            continue
        for yaml_path in iter_queue_files(project.queue_dir):
            try:
                task = parse_task_yaml(
                    yaml_path, project, allow_remote_notify=allow_remote
                )
                ok_count += 1
                warnings.extend(
                    _cap_warnings(
                        conn, name, task, yaml_path,
                        cap_warn_multiplier=cap_warn_multiplier,
                    )
                )
            except Exception as exc:
                errors.append({
                    "project": name,
                    "path": str(yaml_path),
                    "error": str(exc),
                })
    if conn:
        conn.close()
    return {
        "projects": len(projects),
        "ok": ok_count,
        "errors": errors,
        "warnings": warnings,
        "fixed": fixed_info,
        "valid": not errors,
    }
