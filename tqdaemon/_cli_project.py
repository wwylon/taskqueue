"""Project-level CLI commands — extracted from cli.py."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from tqdaemon._cli_helpers import EXIT_UNREACHABLE, _daemon_running, _db, offline_sqlite_write_blocked
from tqdaemon.waiting import print_wait_hint


def _daemon_silent(home: Path) -> bool:
    """True when a daemon runs but did not answer: callers must not write SQLite directly."""
    if _daemon_running(home) is None:
        return False
    print("[ERROR] daemon is running but did not answer; refusing a direct SQLite write.", file=sys.stderr)
    return True


def cmd_sync(
    home: Path,
    *,
    require_daemon: bool = False,
    projects: list[str] | None = None,
    force: bool = False,
) -> int:
    from tqdaemon._cli_helpers import warn_enabled_terminal_summary
    from tqdaemon.db import open_db
    from tqdaemon.master_client import CLI_SYNC_TIMEOUT_SEC, via_master

    body: dict = {}
    if projects:
        body["projects"] = list(projects)
    if force:
        body["force"] = True
    resp = via_master(home, "POST", "/api/cli/sync", body=body or None, timeout=CLI_SYNC_TIMEOUT_SEC)
    if resp is not None:
        if resp.get("unreachable"):
            print(f"[ERROR] {resp.get('error', 'master unreachable')}", file=sys.stderr)
            return EXIT_UNREACHABLE
        if not resp.get("ok"):
            print(f"sync failed: {resp.get('error', resp)}")
            return 1
        per = resp.get("projects") or {}
        for name, count in per.items():
            print(f"  {name}: synced {count} task(s)")
        print(f"Total: {resp.get('total', 0)} task(s)")
        db = open_db(_db(home), migrate=False)
        try:
            warn_enabled_terminal_summary(db)
        finally:
            db.close()
        print_wait_hint()
        return 0
    # Daemon pid alive but HTTP failed (timeout / busy) — do not dual-write SQLite.
    if _daemon_running(home) is not None:
        print(
            "[ERROR] daemon is running but /api/cli/sync did not respond "
            f"(waited {CLI_SYNC_TIMEOUT_SEC:.0f}s). Refusing offline SQLite write.",
            file=sys.stderr,
        )
        return EXIT_UNREACHABLE
    from tqdaemon._cli_helpers import offline_sqlite_write_blocked

    block = offline_sqlite_write_blocked(home)
    if block:
        print(f"[ERROR] {block}", file=sys.stderr)
        return EXIT_UNREACHABLE
    print("[OFFLINE] daemon unreachable; writing SQLite directly (offline repair)", file=sys.stderr)
    if require_daemon:
        print("[OFFLINE] --require-daemon: refusing SQLite write", file=sys.stderr)
        return EXIT_UNREACHABLE
    from tqdaemon.config import load_projects
    from tqdaemon.yaml_sync import _sync_project

    registered = load_projects(home)
    if not registered:
        print("No projects registered. Add entries to ~/.tqdaemon/projects.yaml")
        return 2
    unknown = [n for n in (projects or []) if n not in registered]
    if unknown:
        print(f"Unknown project(s): {', '.join(unknown)}", file=sys.stderr)
        return 1
    # F-118: same reconcile as the daemon (delete-missing, caps, F-079 guards, mtime cache).
    db = open_db(_db(home))
    total_up = 0
    total_del = 0
    for project in registered.values():
        if projects and project.name not in projects:
            continue
        if not project.queue_dir.is_dir():
            print(f"  {project.name}: queue_dir not found ({project.queue_dir}), skipping")
            continue
        stats = _sync_project(db, project, home=home, force=force)
        up = int(stats.get("upserted", 0))
        deleted = int(stats.get("deleted", 0))
        print(f"  {project.name}: upserted {up}, deleted {deleted}")
        total_up += up
        total_del += deleted
    warn_enabled_terminal_summary(db)
    db.close()
    print(f"Total: upserted {total_up}, deleted {total_del}")
    # Exit 2 = offline success (AGENTS / EXIT_USAGE shares the code; stderr has [OFFLINE]).
    return 2


def cmd_add(home: Path, yaml_path: str, project_name: str) -> None:
    from tqdaemon.master_client import via_master

    path = Path(yaml_path).resolve()
    if not path.is_file():
        raise SystemExit(f"YAML not found: {path}")
    yaml_text = path.read_text(encoding="utf-8")
    resp = via_master(
        home,
        "POST",
        "/api/cli/add",
        body={"project": project_name, "yaml_text": yaml_text, "filename": path.name},
    )
    if resp is not None:
        if not resp.get("ok"):
            raise SystemExit(f"add failed: {resp.get('error', resp)}")
        keys = [f"{project_name}/{tid}" for tid in resp.get("task_ids") or []]
        for key in keys:
            print(f"Added: {key}")
        if keys:
            print_wait_hint(keys, project=project_name)
        return
    if _daemon_silent(home):
        raise SystemExit(EXIT_UNREACHABLE)
    print("[tq] daemon unreachable; writing SQLite directly (offline repair)", file=sys.stderr)
    from tqdaemon.config import load_projects
    from tqdaemon.db import get_config, open_db, parse_task_yaml_many, upsert_task

    projects = load_projects(home)
    project = projects.get(project_name)
    if project is None:
        raise SystemExit(f"Unknown project '{project_name}'. Add it to ~/.tqdaemon/projects.yaml")
    path = Path(yaml_path).resolve()
    db = open_db(_db(home))
    allow_remote = bool(get_config(db).get("notify_allow_remote"))
    tasks = parse_task_yaml_many(path, project, allow_remote_notify=allow_remote)
    from tqdaemon.db import stamp_provenance

    for task in tasks:
        upsert_task(db, task)
        stamp_provenance(
            db,
            project_name,
            [task.task_id],
            "cli",
            os.environ.get("TQ_OPERATOR"),
            "tq-cli (offline)",
        )
        print(f"Added: {project_name}/{task.task_id} (priority={task.priority})")
    db.close()
    if tasks:
        print_wait_hint([f"{project_name}/{t.task_id}" for t in tasks], project=project_name)


def _project_exists(home: Path, project: str) -> bool:
    from tqdaemon.config import load_projects

    if project in load_projects(home):
        return True
    print(f"Unknown project '{project}'.", file=sys.stderr)
    return False


def cmd_pause(home: Path, project: str) -> int:
    """Pause a project: the scheduler skips its queued tasks (via the master when a daemon runs)."""
    if not _project_exists(home, project):
        return 1
    from tqdaemon._cli_helpers import EXIT_UNREACHABLE, master_mutation

    resp, mode = master_mutation(home, f"/api/project/pause/{project}")
    if mode == "unreachable":
        return EXIT_UNREACHABLE
    if mode == "master":
        if not resp.get("ok"):
            print(f"Error: {resp.get('error', 'failed')}", file=sys.stderr)
            return 1
    else:
        from tqdaemon.db import open_db, set_project_paused

        db = open_db(_db(home))
        set_project_paused(db, project, True)
        db.close()
    print(f"Project {project} paused (scheduler will skip its queued tasks).")
    return 0


def cmd_resume_project(home: Path, project: str) -> int:
    """Resume a paused project."""
    if not _project_exists(home, project):
        return 1
    from tqdaemon._cli_helpers import EXIT_UNREACHABLE, master_mutation

    resp, mode = master_mutation(home, f"/api/project/resume/{project}")
    if mode == "unreachable":
        return EXIT_UNREACHABLE
    if mode == "master":
        if not resp.get("ok"):
            print(f"Error: {resp.get('error', 'failed')}", file=sys.stderr)
            return 1
    else:
        from tqdaemon.db import open_db, set_project_paused

        db = open_db(_db(home))
        set_project_paused(db, project, False)
        db.close()
    print(f"Project {project} resumed.")
    return 0


def cmd_freeze(home: Path, project: str) -> int:
    """W6.1: Freeze a project — scheduler completely skips it (stronger than pause)."""
    if not _project_exists(home, project):
        return 1
    from tqdaemon._cli_helpers import EXIT_UNREACHABLE, master_mutation

    resp, mode = master_mutation(home, f"/api/project/freeze/{project}")
    if mode == "unreachable":
        return EXIT_UNREACHABLE
    if mode == "master":
        if not resp.get("ok"):
            print(f"Error: {resp.get('error', 'failed')}", file=sys.stderr)
            return 1
    else:
        from tqdaemon.db import open_db, set_project_frozen

        db = open_db(_db(home))
        set_project_frozen(db, project, True)
        db.close()
    print(f"Project {project} frozen. Use 'tq unfreeze {project}' to resume scheduling.")
    return 0


def cmd_unfreeze(home: Path, project: str) -> int:
    """W6.1: Unfreeze a project."""
    if not _project_exists(home, project):
        return 1
    from tqdaemon._cli_helpers import EXIT_UNREACHABLE, master_mutation

    resp, mode = master_mutation(home, f"/api/project/unfreeze/{project}")
    if mode == "unreachable":
        return EXIT_UNREACHABLE
    if mode == "master":
        if not resp.get("ok"):
            print(f"Error: {resp.get('error', 'failed')}", file=sys.stderr)
            return 1
    else:
        from tqdaemon.db import open_db, set_project_frozen

        db = open_db(_db(home))
        set_project_frozen(db, project, False)
        db.close()
    print(f"Project {project} unfrozen.")
    return 0


def cmd_release(home: Path, project: str) -> int:
    """F-240: drop the daemon's queue-dir watch so the project folder can be renamed."""
    if not _project_exists(home, project):
        return 1
    from tqdaemon._cli_helpers import EXIT_UNREACHABLE, master_mutation

    resp, mode = master_mutation(home, f"/api/project/release/{project}")
    if mode == "unreachable":
        return EXIT_UNREACHABLE
    if mode == "master":
        if not resp or not resp.get("ok"):
            print(f"Error: {(resp or {}).get('error', 'failed')}", file=sys.stderr)
            return 1
        print(f"Project {project} released (queue-dir watch dropped; safe to rename the folder).")
        return 0
    # Offline: nothing holds a watch.
    print(f"Project {project}: no daemon running — nothing to release (rename is already free).")
    return 0


def cmd_relocate(home: Path, project: str, new_root: str) -> int:
    """F-240: rewrite projects.yaml root + yaml_source after the folder has moved."""
    if not _project_exists(home, project):
        return 1
    from tqdaemon._cli_helpers import EXIT_UNREACHABLE, master_mutation

    new_path = Path(new_root)
    resp, mode = master_mutation(
        home, f"/api/project/relocate/{project}", body={"new_root": str(new_path)}
    )
    if mode == "unreachable":
        return EXIT_UNREACHABLE
    if mode == "master":
        if not resp or not resp.get("ok"):
            print(f"Error: {(resp or {}).get('error', 'failed')}", file=sys.stderr)
            return 1
        before = resp.get("before") or {}
        after = resp.get("after") or {}
        print(
            f"Project {project} relocated: {resp.get('old_root')} -> {resp.get('new_root')} "
            f"(tasks {before.get('count')}->{after.get('count')}, "
            f"yaml_source rewrites={resp.get('yaml_source_rewritten', 0)}, "
            f"backup={resp.get('backup')})"
        )
        return 0
    # Offline path: rewrite files + DB directly, then sync.
    from tqdaemon.db import open_db
    from tqdaemon.project_relocate import (
        count_running,
        relocate_project_files,
        status_histogram,
    )
    from tqdaemon.yaml_sync import _sync_project

    db = open_db(_db(home))
    try:
        n_run = count_running(db, project)
        if n_run:
            print(f"Error: {n_run} task(s) running in {project}; stop them first", file=sys.stderr)
            return 1
        try:
            summary = relocate_project_files(home, db, project, new_path)
        except (KeyError, ValueError, FileNotFoundError, OSError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        proj = summary.pop("project_obj")
        sync_stats = _sync_project(db, proj, home=home, force=True)
        after = status_histogram(db, project)
        after_n = sum(after.values())
        before = summary["before"]
        matched = before["count"] == after_n and before["status"] == after
        print(
            f"Project {project} relocated (offline): {summary['old_root']} -> {summary['new_root']} "
            f"(tasks {before['count']}->{after_n}, yaml_source rewrites={summary['yaml_source_rewritten']}, "
            f"backup={summary['backup']}, sync={sync_stats})"
        )
        if not matched:
            print("Error: task count/status mismatch after relocate sync", file=sys.stderr)
            return 1
        return 0
    finally:
        db.close()


def cmd_retry_project(
    home: Path,
    project: str,
    *,
    status_filter: str | None = None,
) -> int:
    """Retry retryable tasks in a project (optionally filtered by status)."""
    from tqdaemon.db import RETRYABLE_STATUSES, get_all_tasks, open_db, retry_tasks_batch
    from tqdaemon.master_client import via_master

    statuses = set(RETRYABLE_STATUSES)
    if status_filter:
        wanted = {s.strip() for s in status_filter.split(",") if s.strip()}
        bad = wanted - set(RETRYABLE_STATUSES)
        if bad:
            print(f"Unknown status filter: {', '.join(sorted(bad))}")
            print(f"Allowed: {', '.join(sorted(RETRYABLE_STATUSES))}")
            return 2
        statuses = wanted

    body = {"project": project, "status": sorted(statuses)}
    resp = via_master(home, "POST", "/api/tasks/retry-batch", body=body)
    if resp is not None:
        # F-197: via_master returns {"unreachable": true} (not None) on timeout.
        if resp.get("unreachable"):
            print(f"[ERROR] {resp.get('error', 'master unreachable')}", file=sys.stderr)
            return EXIT_UNREACHABLE
        n = len(resp.get("requeued") or [])
        print(f"Re-queued {n} task(s) in {project}.")
        for sk in resp.get("skipped") or []:
            print(f"  skip {sk.get('key')}: {sk.get('reason')}")
        if n:
            print_wait_hint(project=project)
        return 0

    if _daemon_silent(home):
        return EXIT_UNREACHABLE
    db = open_db(_db(home))
    keys = [(row["project"], row["task_id"]) for row in get_all_tasks(db, project=project) if row["status"] in statuses]
    result = retry_tasks_batch(db, keys)
    db.close()
    print(f"Re-queued {len(result['requeued'])} task(s) in {project}.")
    for sk in result["skipped"]:
        print(f"  skip {sk['key']}: {sk['reason']}")
    if result["requeued"]:
        print_wait_hint(project=project)
    return 0


def cmd_retry_keys(home: Path, keys: list[str]) -> int:
    """Batch-retry multiple project/task_id keys in one transaction."""
    from tqdaemon.db import open_db, retry_tasks_batch
    from tqdaemon.master_client import via_master

    parsed: list[tuple[str, str]] = []
    for key in keys:
        if "/" not in key:
            print(f"Usage: tq retry <project/task_id>… (bad key: {key})")
            return 1
        project, task_id = key.split("/", 1)
        parsed.append((project, task_id))

    body = {"task_ids": [f"{p}/{t}" for p, t in parsed]}
    resp = via_master(home, "POST", "/api/tasks/retry-batch", body=body)
    if resp is not None:
        # F-197: do not treat unreachable JSON as a successful empty retry.
        if resp.get("unreachable"):
            print(f"[ERROR] {resp.get('error', 'master unreachable')}", file=sys.stderr)
            return EXIT_UNREACHABLE
        n = len(resp.get("requeued") or [])
        print(f"Re-queued {n} task(s).")
        for sk in resp.get("skipped") or []:
            print(f"  skip {sk.get('key')}: {sk.get('reason')}")
        if n:
            print_wait_hint(resp.get("requeued") or keys)
        return 0 if n else (1 if resp.get("skipped") else 0)

    if _daemon_silent(home):
        return EXIT_UNREACHABLE
    db = open_db(_db(home))
    result = retry_tasks_batch(db, parsed)
    db.close()
    n = len(result["requeued"])
    print(f"Re-queued {n} task(s).")
    for sk in result["skipped"]:
        print(f"  skip {sk['key']}: {sk['reason']}")
    if n:
        print_wait_hint(result["requeued"])
    return 0 if n else (1 if result["skipped"] else 0)


def cmd_validate(
    home: Path,
    path: str | None = None,
    *,
    fix: bool = False,
) -> int:
    """Ext.1: when path given, lint only that file; no-arg form lints all registered projects.

    ``--fix`` rewrites known legacy top-level keys in place (may drop comments).
    """
    if path:
        from tqdaemon.validate import validate_file

        p = Path(path)
        if not p.exists():
            print(f"tq validate: file not found: {path}")
            return 1
        report = validate_file(p, home, fix=fix)
    else:
        from tqdaemon.validate import validate_home

        report = validate_home(home, fix=fix)
    warnings = report.get("warnings") or []
    fixed = report.get("fixed") or []
    print(
        f"projects={report['projects']} ok_yaml={report['ok']} "
        f"errors={len(report['errors'])} warnings={len(warnings)}" + (f" fixed={len(fixed)}" if fix or fixed else "")
    )
    for item in fixed:
        print(f"  FIX {item['project']}: {item['path']}")
        for ch in item.get("changes") or []:
            print(f"       {ch}")
    for err in report["errors"]:
        print(f"  !! {err['project']}: {err['path']}: {err['error']}")
    for w in warnings:
        print(f"  WW {w['project']}/{w['task_id']}: {w['warning']}")
    return 0 if report["valid"] else 1


def cmd_projects(home: Path, validate: bool = False) -> int:
    from tqdaemon.config import load_projects
    from tqdaemon.db import is_project_frozen, is_project_paused, open_db

    projects = load_projects(home)
    if not projects:
        print("No projects registered. Create ~/.tqdaemon/projects.yaml")
        return 1 if validate else 0
    db_path = _db(home)
    conn = open_db(db_path) if db_path.exists() else None
    errors = 0
    for name, p in projects.items():
        root_ok = p.root.is_dir()
        queue_ok = p.queue_dir.is_dir()
        root_flag = "✓" if root_ok else "✗"
        queue_flag = "ok" if queue_ok else "missing"
        state_flags = []
        if conn:
            if is_project_frozen(conn, name):
                state_flags.append("frozen")
            elif is_project_paused(conn, name):
                state_flags.append("paused")
        state_str = f"  [{', '.join(state_flags)}]" if state_flags else ""
        set_str = f"  set={p.set_path}" if p.set_path else ""
        print(f"  {name:20s} {root_flag} {str(p.root):50s}  queue={queue_flag}{set_str}{state_str}")
        if not root_ok:
            errors += 1
            if validate:
                print(f"    ERROR: root directory does not exist: {p.root}")
    if conn:
        conn.close()
    if validate and errors:
        print(f"\n{errors} project(s) have missing root directories.")
        return 1
    return 0


def cmd_set(home: Path, rest: list[str]) -> int:
    """tq set tree|show|assign|clear|rename|move|limits|limits-show|freeze|…"""
    if not rest:
        print(
            "Usage: tq set tree|show <path>|assign <project> <path>|clear <project>|"
            "rename <old> <new>|move <old> <new>|limits <path> [field=val…]|"
            "limits-show <path>|freeze|unfreeze|pause|resume|stop <path>"
        )
        return 1
    verb = rest[0]
    args = rest[1:]
    if verb == "tree":
        return _cmd_set_tree(home)
    if verb == "show":
        if not args:
            print("Usage: tq set show <path>")
            return 1
        return _cmd_set_show(home, args[0])
    if verb == "assign":
        if len(args) < 2:
            print("Usage: tq set assign <project> <path>")
            return 1
        return _cmd_set_assign(home, args[0], args[1])
    if verb == "clear":
        if not args:
            print("Usage: tq set clear <project>")
            return 1
        return _cmd_set_assign(home, args[0], None)
    if verb in ("rename", "move"):
        if len(args) < 2:
            print(f"Usage: tq set {verb} <old> <new>")
            return 1
        return _cmd_set_rename(home, args[0], args[1])
    if verb == "limits-show":
        if not args:
            print("Usage: tq set limits-show <path>")
            return 1
        return _cmd_set_limits_show(home, args[0])
    if verb == "limits":
        if not args:
            print("Usage: tq set limits <path> [field=val …]")
            return 1
        return _cmd_set_limits(home, args[0], args[1:])
    if verb in ("freeze", "unfreeze", "pause", "resume", "stop"):
        if not args:
            print(f"Usage: tq set {verb} <path>")
            return 1
        action = "stop-all" if verb == "stop" else verb
        return _cmd_set_bulk(home, action, args[0])
    print(f"Unknown tq set subcommand: {verb}")
    return 1


def _cmd_set_tree(home: Path) -> int:
    from tqdaemon.config import load_projects
    from tqdaemon.project_sets import build_tree, unset_projects

    projects = load_projects(home)
    tree = build_tree(projects)

    def walk(nodes: list, indent: int = 0) -> None:
        for node in nodes:
            pad = "  " * indent
            print(f"{pad}{node['path']}  ({node['subtree_count']} project(s))")
            for pname in node.get("direct_projects") or []:
                print(f"{pad}  · {pname}")
            walk(node.get("children") or [], indent + 1)

    roots = tree.get("children") or []
    if not roots:
        print("(no project sets)")
    else:
        walk(roots)
    unset = unset_projects(projects)
    if unset:
        print(f"unset  ({len(unset)} project(s))")
        for pname in unset:
            print(f"  · {pname}")
    return 0


def _cmd_set_show(home: Path, path: str) -> int:
    from tqdaemon.config import load_projects
    from tqdaemon.db import is_project_frozen, is_project_paused, open_db
    from tqdaemon.project_sets import node_exists, normalize_path, projects_in_subtree

    try:
        node = normalize_path(path)
    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    assert node is not None
    projects = load_projects(home)
    if not node_exists(projects, node):
        print(f"[ERROR] unknown set path: {node}", file=sys.stderr)
        return 1
    members = projects_in_subtree(projects, node)
    db_path = _db(home)
    conn = open_db(db_path) if db_path.exists() else None
    print(f"{node}: {len(members)} project(s)")
    for name in members:
        flags = []
        if conn:
            if is_project_frozen(conn, name):
                flags.append("frozen")
            if is_project_paused(conn, name):
                flags.append("paused")
            sp = projects[name].set_path
            if sp and sp != node:
                flags.append(f"set={sp}")
        extra = f"  [{', '.join(flags)}]" if flags else ""
        print(f"  {name}{extra}")
    if conn:
        conn.close()
    return 0


def _cmd_set_assign(home: Path, project: str, path: str | None) -> int:
    from tqdaemon.config import load_projects
    from tqdaemon.project_sets import normalize_path, set_project_set

    if project not in load_projects(home):
        print(f"[ERROR] project {project!r} not registered", file=sys.stderr)
        return 1
    try:
        path_n = normalize_path(path) if path is not None else None
        prev = set_project_set(home, project, path_n)
    except (KeyError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    if path_n is None:
        print(f"Cleared set for {project}" + (f" (was {prev})" if prev else ""))
    else:
        print(f"Assigned {project} → {path_n}" + (f" (was {prev})" if prev else ""))
    return 0


def _cmd_set_rename(home: Path, old: str, new: str) -> int:
    from tqdaemon.project_sets import rename_set_prefix

    try:
        affected = rename_set_prefix(home, old, new)
    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    print(f"Renamed {old} → {new}: {len(affected)} project(s)")
    for name in affected:
        print(f"  {name}")
    return 0


def _cmd_set_limits_show(home: Path, path: str) -> int:
    from tqdaemon.config import load_projects
    from tqdaemon.db import get_project_limits, get_set_limits, open_db
    from tqdaemon.project_limits import (
        _PROJECT_LIMIT_FIELDS,
        resolve_effective_limits,
    )
    from tqdaemon.project_sets import node_exists, normalize_path, projects_in_subtree

    try:
        node = normalize_path(path)
    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    assert node is not None
    projects = load_projects(home)
    if not node_exists(projects, node):
        print(f"[ERROR] unknown set path: {node}", file=sys.stderr)
        return 1
    db_path = _db(home)
    if not db_path.exists():
        print("[ERROR] no queue database", file=sys.stderr)
        return 1
    conn = open_db(db_path)
    try:
        lim = get_set_limits(conn, node)
        print(f"set {node} defaults:")
        for f in _PROJECT_LIMIT_FIELDS:
            print(f"  {f}={lim.get(f, 0)}")
        members = projects_in_subtree(projects, node)
        if members:
            print("effective for members:")
            for name in members:
                ov = get_project_limits(conn, name)
                eff, src = resolve_effective_limits(ov, projects[name].set_path, conn=conn)
                bits = [f"{f}={eff[f]}({src[f]})" for f in _PROJECT_LIMIT_FIELDS if eff[f]]
                print(f"  {name}: " + (", ".join(bits) if bits else "(unlimited)"))
    finally:
        conn.close()
    return 0


def _cmd_set_limits(home: Path, path: str, kv_args: list[str]) -> int:
    from tqdaemon._cli_helpers import EXIT_UNREACHABLE, master_mutation
    from tqdaemon.project_limits import _PROJECT_LIMIT_FIELDS
    from tqdaemon.project_sets import normalize_path

    try:
        node = normalize_path(path)
    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    assert node is not None
    body: dict = {"set": node}
    if not kv_args:
        print("Usage: tq set limits <path> field=val …", file=sys.stderr)
        return 1
    for item in kv_args:
        if "=" not in item:
            print(f"[ERROR] expected field=val, got {item!r}", file=sys.stderr)
            return 1
        k, _, v = item.partition("=")
        if k not in _PROJECT_LIMIT_FIELDS:
            print(f"[ERROR] unknown field {k!r}; choose from {', '.join(_PROJECT_LIMIT_FIELDS)}", file=sys.stderr)
            return 1
        try:
            body[k] = float(v)
        except ValueError:
            print(f"[ERROR] bad value for {k}: {v!r}", file=sys.stderr)
            return 1
    resp, mode = master_mutation(home, "/api/set-limits", body=body)
    if mode == "unreachable":
        return EXIT_UNREACHABLE
    if mode == "master":
        if not resp or not resp.get("ok"):
            print(f"[ERROR] {(resp or {}).get('error', 'failed')}", file=sys.stderr)
            return 1
        print(f"set limits {node}: " + ", ".join(f"{k}={body[k]}" for k in body if k != "set"))
        return 0
    from tqdaemon.db import open_db, set_set_limit

    print(f"[OFFLINE] set limits {node}", file=sys.stderr)
    db = open_db(_db(home))
    try:
        for k, v in body.items():
            if k == "set":
                continue
            set_set_limit(db, node, k, float(v))
            print(f"  {k}={v}")
    finally:
        db.close()
    return 0


def _cmd_set_bulk(home: Path, action: str, path: str) -> int:
    from tqdaemon._cli_helpers import EXIT_UNREACHABLE, master_mutation

    resp, mode = master_mutation(home, f"/api/project-set/{action}", body={"set": path})
    if mode == "unreachable":
        return EXIT_UNREACHABLE
    if mode == "master":
        if not resp or not resp.get("ok"):
            print(f"[ERROR] {(resp or {}).get('error', 'failed')}", file=sys.stderr)
            return 1
        n = len(resp.get("projects") or [])
        extra = f", stopped={resp.get('stopped')}" if action == "stop-all" else ""
        print(f"{action} set {path}: {n} project(s){extra}")
        return 0
    from tqdaemon.config import load_projects
    from tqdaemon.db import (
        get_running_tasks,
        open_db,
        request_stop_with_control,
        set_project_frozen,
        set_project_paused,
    )
    from tqdaemon.project_sets import node_exists, normalize_path, projects_in_subtree

    try:
        node = normalize_path(path)
    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    assert node is not None
    projects = load_projects(home)
    if not node_exists(projects, node):
        print(f"[ERROR] unknown set path: {node}", file=sys.stderr)
        return 1
    members = projects_in_subtree(projects, node)
    if not members:
        print(f"[ERROR] set path {node!r} has no projects", file=sys.stderr)
        return 1
    print(f"[OFFLINE] {action} set {node} ({len(members)} project(s))", file=sys.stderr)
    db = open_db(_db(home))
    try:
        if action in ("freeze", "unfreeze"):
            for name in members:
                set_project_frozen(db, name, action == "freeze")
                print(f"  {action} {name}")
        elif action in ("pause", "resume"):
            for name in members:
                set_project_paused(db, name, action == "pause")
                print(f"  {action} {name}")
        elif action == "stop-all":
            member_set = set(members)
            stopped = 0
            for row in get_running_tasks(db):
                if row["project"] in member_set:
                    request_stop_with_control(db, home, row["project"], row["task_id"])
                    stopped += 1
            print(f"  stopped={stopped}")
    finally:
        db.close()
    return 0


def cmd_bundle(home: Path, project_name: str) -> int:
    from tqdaemon.bundle import create_project_bundle
    from tqdaemon.config import load_projects

    projects = load_projects(home)
    project = projects.get(project_name)
    if not project:
        print(f"Error: project '{project_name}' not registered on coordinator.")
        return 1

    print(f"[bundle] Packaging project '{project_name}' at {project.root}...")
    try:
        bundle_hash, dest, files_added = create_project_bundle(home, project, project_name=project_name)
    except ValueError as exc:
        print(f"[bundle] Error: {exc}")
        return 1
    print(f"[bundle] Bundle created successfully! ({files_added} files packed)")
    print(f"[bundle] Hash: {bundle_hash}")
    print(f"[bundle] Saved to: {dest}")
    return 0


def _suite_set_enabled(home: Path, conn, suite_name: str, value: bool) -> int:
    """Flip enabled for a suite by patching each task YAML (so resync keeps it).

    Tasks without a readable yaml_source fall back to a direct DB update.
    """
    from tqdaemon.config import load_projects
    from tqdaemon.db import get_config, parse_task_yaml, upsert_task
    from tqdaemon.yaml_patch import patch_task_yaml

    projects = load_projects(home)
    allow_remote = bool(get_config(conn).get("notify_allow_remote"))
    rows = conn.execute("SELECT project, task_id, yaml_source FROM tasks WHERE suite=?", (suite_name,)).fetchall()
    n = 0
    for row in rows:
        proj = projects.get(row["project"])
        path = Path(row["yaml_source"]) if row["yaml_source"] else None
        if proj is not None and path is not None and path.is_file():
            try:
                patch_task_yaml(path, {"enabled": value}, task_id=row["task_id"])
                upsert_task(conn, parse_task_yaml(path, proj, allow_remote_notify=allow_remote))
                n += 1
                continue
            except Exception as exc:  # noqa: BLE001 - one task failing is reported; the rest continue
                print(f"[WARN] YAML patch failed for {row['project']}/{row['task_id']}: {exc}")
        conn.execute(
            "UPDATE tasks SET enabled=? WHERE project=? AND task_id=?",
            (1 if value else 0, row["project"], row["task_id"]),
        )
        conn.commit()
        n += 1
    return n


def cmd_suite(home: Path, action: str, suite_name: str) -> int:
    """I-08: Suite-level enable/disable/retry."""
    from tqdaemon.db import open_db, retry_task

    db = _db(home)
    if not db.exists():
        print("No queue DB.")
        return 1
    if action in ("enable", "disable", "retry"):
        block = offline_sqlite_write_blocked(home)
        if block:
            print(f"[ERROR] {block}", file=sys.stderr)
            return EXIT_UNREACHABLE
    conn = open_db(db)
    if action in ("enable", "disable"):
        n = _suite_set_enabled(home, conn, suite_name, action == "enable")
        verb = "Enabled" if action == "enable" else "Disabled"
        print(f"{verb} {n} task(s) in suite '{suite_name}'.")
    elif action == "retry":
        from tqdaemon._db_tasks import RETRYABLE_STATUSES

        placeholders = ",".join("?" * len(RETRYABLE_STATUSES))
        rows = conn.execute(
            f"SELECT project, task_id, status FROM tasks WHERE suite=? AND status IN ({placeholders})",
            (suite_name, *sorted(RETRYABLE_STATUSES)),
        ).fetchall()
        n = 0
        for row in rows:
            if retry_task(conn, row["project"], row["task_id"]):
                n += 1
        print(f"Re-queued {n} task(s) in suite '{suite_name}'.")
    else:
        print(f"Unknown suite action '{action}'. Use: enable, disable, retry")
        conn.close()
        return 1
    conn.close()
    return 0


def cmd_resync(
    home: Path,
    project: str,
    *,
    worker_id: str | None = None,
    all_workers: bool = False,
    auth_token: str | None = None,
) -> int:
    """Bump sync generation and optionally queue file sync on workers."""
    from tqdaemon.config import load_projects
    from tqdaemon.db import get_config, open_db
    from tqdaemon.project_sync import bump_sync_generation

    projects = load_projects(home)
    if project not in projects:
        print(f"Unknown project '{project}'.", file=sys.stderr)
        return 1
    gen = bump_sync_generation(home, project)
    print(f"Bumped sync generation for '{project}' → {gen}")

    conn = open_db(_db(home))
    try:
        cfg = get_config(conn)
        port = int(cfg.get("web_port", 8765))
        token = auth_token or os.environ.get("TQ_AUTH_TOKEN") or str(cfg.get("auth_token") or "")
        worker_ids: list[str] = []
        if worker_id:
            worker_ids = [worker_id]
        elif all_workers:
            rows = conn.execute("SELECT worker_id FROM workers WHERE status = 'online'").fetchall()
            worker_ids = [str(r["worker_id"]) for r in rows]
    finally:
        conn.close()

    if not worker_ids:
        print("Workers will re-diff on next remote task claim (fingerprint mismatch).")
        print("To warm a worker now: tq resync <project> --worker <id>  (or --all-workers)")
        return 0

    from tqdaemon.master_client import post_json

    master_url = f"http://127.0.0.1:{port}"
    ok = 0
    for wid in worker_ids:
        try:
            result = post_json(master_url, f"/api/worker/file-sync/{wid}", token=token)
            if result.get("ok"):
                print(f"Queued file sync for worker '{wid}' (next heartbeat).")
                ok += 1
            else:
                print(f"Worker '{wid}': {result.get('error', 'failed')}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001 - any network error is reported per worker
            print(f"Worker '{wid}': {e}", file=sys.stderr)
    return 0 if ok == len(worker_ids) else 1


def cmd_archive(
    home: Path,
    *,
    projects: list[str] | None = None,
    older_than_days: float = 30.0,
    statuses: list[str] | None = None,
    dry_run: bool = False,
    max_completed_runs: int | None = None,
) -> int:
    """Archive old finished tasks (JSONL export + YAML moved aside + DB rows dropped)."""
    from tqdaemon.master_client import via_master

    body: dict = {"older_than_days": older_than_days, "dry_run": dry_run}
    if projects:
        body["projects"] = list(projects)
    if statuses:
        body["statuses"] = list(statuses)
    if max_completed_runs is not None:
        body["max_completed_runs"] = int(max_completed_runs)
    resp = via_master(home, "POST", "/api/cli/archive", body=body, timeout=120.0)
    if resp is None:
        if _daemon_running(home) is not None:
            print("[ERROR] daemon is running but did not answer; refusing a direct SQLite write.", file=sys.stderr)
            return EXIT_UNREACHABLE
        from tqdaemon.archive import archive_tasks
        from tqdaemon.config import load_projects
        from tqdaemon.db import open_db

        registered = load_projects(home)
        unknown = [n for n in (projects or []) if n not in registered]
        if unknown:
            print(f"Unknown project(s): {', '.join(unknown)}", file=sys.stderr)
            return 1
        db = open_db(_db(home))
        try:
            resp = {
                "ok": True,
                **archive_tasks(
                    db,
                    registered,
                    home=home,
                    older_than_days=older_than_days,
                    statuses=tuple(statuses or ("completed",)),
                    only_projects=projects or None,
                    dry_run=dry_run,
                    max_completed_runs=max_completed_runs,
                ),
            }
        finally:
            db.close()
    if not resp.get("ok"):
        print(f"archive failed: {resp.get('error', resp)}")
        return 1
    verb = "Would archive" if resp.get("dry_run") else "Archived"
    for t in resp.get("tasks", [])[:50]:
        print(f"  {t['project']}/{t['task_id']}")
    extra = len(resp.get("tasks", [])) - 50
    if extra > 0:
        print(f"  ... and {extra} more")
    print(f"{verb} {resp.get('archived', 0)} task(s).")
    return 0
