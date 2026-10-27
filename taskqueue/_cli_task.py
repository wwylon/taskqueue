"""Task-level CLI commands — extracted from cli.py."""
from __future__ import annotations
from taskqueue._time import parse_iso as _parse_iso
from taskqueue._db_tasks import get_task, get_task_status
from taskqueue.jsonutil import json_list
import json
import os
import sys

from taskqueue._cli_helpers import EXIT_UNREACHABLE
from pathlib import Path
from taskqueue._cli_helpers import (
    emit_json,
    _db,
    _split_key,
    _check_ownership_or_print,
    format_resources,
    warn_enabled_but_terminal,
    last_nonempty_line,
    read_last_lines,
)
from taskqueue.rows import row_get

def _list_fetch_rows(
    conn,
    project: str | None,
    *,
    tags: list[str] | None = None,
    suite: str | None = None,
    status_filter: str | None = None,
    worker_filter: str | None = None,
) -> tuple[list, dict]:
    """Shared DB query + tag filter + lock occupancy for cmd_list."""
    clauses: list[str] = []
    params: list = []
    if project:
        clauses.append("tasks.project=?")
        params.append(project)
    if suite:
        clauses.append("tasks.suite=?")
        params.append(suite)
    # I-03: status filter (single value or comma-separated list)
    if status_filter:
        statuses = [s.strip() for s in status_filter.split(",") if s.strip()]
        if len(statuses) == 1:
            clauses.append("tasks.status=?")
            params.append(statuses[0])
        elif statuses:
            placeholders = ",".join("?" * len(statuses))
            clauses.append(f"tasks.status IN ({placeholders})")
            params.extend(statuses)
    # worker filter: join on most recent run's worker_id
    if worker_filter:
        clauses.append("r.worker_id=?")
        params.append(worker_filter)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    # R1b: fetch extended fields including run-level data and attempt count
    rows = conn.execute(
        f"SELECT tasks.project, tasks.task_id, tasks.priority, tasks.status, tasks.enabled,"
        f" tasks.resources, tasks.last_error_code, tasks.block_reason, tasks.description,"
        f" tasks.tags, tasks.suite, tasks.grp, tasks.metadata, tasks.artifacts, tasks.depends_on,"
        f" tasks.depends_on_any, tasks.spawned_by,"
        f" r.last_log_line, r.worker_id AS last_worker_id, r.progress_pct,"
        f" r.exit_code, r.started_at AS run_started_at, r.finished_at AS run_finished_at, r.log_path,"
        f" rc.attempts"
        f" FROM tasks"
        f" LEFT JOIN runs r ON r.run_id = tasks.last_run_id"
        f" LEFT JOIN ("
        f"   SELECT project, task_id, COUNT(*) AS attempts"
        f"   FROM runs GROUP BY project, task_id"
        f" ) rc ON rc.project=tasks.project AND rc.task_id=tasks.task_id"
        f" {where} ORDER BY tasks.priority DESC, tasks.enqueued_at ASC",
        params,
    ).fetchall()
    # I-15: client-side compound tag filter — all requested tags must be present (AND logic)
    if tags:
        rows = [r for r in rows if all(t in json_list(r["tags"]) for t in tags)]
    from taskqueue.db import lock_occupancy
    occupancy = lock_occupancy(conn)
    return rows, occupancy


def _list_json(rows, occupancy: dict, home: Path, *, stat_artifacts: bool = False) -> None:
    from taskqueue.config import load_projects as _load_projects
    from taskqueue.resources import task_requires_gpu
    _projects = _load_projects(home) if stat_artifacts else {}
    out = []
    for r in rows:
        res = json.loads(r["resources"] or "{}")
        artifact_paths = json_list(r["artifacts"]) if "artifacts" in r.keys() else []
        artifacts_info: list = []
        if stat_artifacts and artifact_paths:
            proj_obj = _projects.get(r["project"])
            for ap in artifact_paths:
                p = proj_obj.resolve(ap) if proj_obj else Path(ap)
                try:
                    st = p.stat()
                    artifacts_info.append({"path": ap, "exists": True, "size": st.st_size, "mtime": st.st_mtime})
                except OSError:
                    artifacts_info.append({"path": ap, "exists": False, "size": None, "mtime": None})
        elif artifact_paths:
            artifacts_info = [{"path": ap} for ap in artifact_paths]
        lock_waiting_on = []
        err_code = row_get(r, "last_error_code")
        if err_code == "tq.block.mutex":
            for lk in (res.get("locks") or []):
                info = occupancy.get(str(lk)) or {}
                lock_waiting_on.append({
                    "lock": str(lk),
                    "holder": info.get("holder"),
                    "holder_started_at": info.get("holder_started_at"),
                })
        out.append({
            "project": r["project"],
            "task_id": r["task_id"],
            "priority": r["priority"],
            "status": r["status"],
            "enabled": bool(r["enabled"]) if "enabled" in r.keys() and r["enabled"] is not None else True,
            "resources": res,
            "requires_gpu": task_requires_gpu(res),
            "last_error_code": err_code,
            "block_reason": row_get(r, "block_reason"),
            "description": row_get(r, "description"),
            "tags": json_list(r["tags"]),
            "suite": r["suite"] or "",
            "group": r["grp"] or "" if "grp" in r.keys() else "",
            "last_log_line": row_get(r, "last_log_line"),
            "progress_pct": row_get(r, "progress_pct"),
            "metadata": json.loads(r["metadata"] or "{}") if "metadata" in r.keys() else {},
            "artifacts": artifacts_info,
            "depends_on": json_list(r["depends_on"]) if "depends_on" in r.keys() else [],
            "depends_on_any": json_list(r["depends_on_any"]) if "depends_on_any" in r.keys() else [],
            "spawned_by": r["spawned_by"] if "spawned_by" in r.keys() and r["spawned_by"] else None,
            "exit_code": row_get(r, "exit_code"),
            "attempts": row_get(r, "attempts"),
            "started_at": row_get(r, "run_started_at"),
            "finished_at": row_get(r, "run_finished_at"),
            "log_path": row_get(r, "log_path"),
            "lock_waiting_on": lock_waiting_on or None,
            "cap_auto_retry_count": int(r["cap_auto_retry_count"] or 0) if "cap_auto_retry_count" in r.keys() else 0,
            "max_auto_retries": int(r["max_auto_retries"] if "max_auto_retries" in r.keys() and r["max_auto_retries"] is not None else 20),
            "auto_retry_on_cap": bool(int(r["auto_retry_on_cap"] or 0)) if "auto_retry_on_cap" in r.keys() else False,
        })
    emit_json(out, ensure_ascii=False)


def _list_row_display(r, occupancy: dict) -> tuple[str, str, str, str | None, str | None, str]:
    """Shared per-row display fields for verbose/compact human output."""
    res = json.loads(r["resources"] or "{}")
    res_str = format_resources(res)
    err = row_get(r, "last_error_code")
    reason = row_get(r, "block_reason")
    last_log = row_get(r, "last_log_line")
    enabled = bool(r["enabled"]) if "enabled" in r.keys() and r["enabled"] is not None else True
    pct = row_get(r, "progress_pct")
    status = r["status"]
    if status == "queued" and err and str(err).startswith("tq.block."):
        status = "waiting"
    err_str = f"  [{err}]" if err else ""
    off_str = " [yaml-off]" if (not enabled and r["status"] != "disabled") else ""
    cap_str = ""
    try:
        cap_n = int(r["cap_auto_retry_count"] or 0) if "cap_auto_retry_count" in r.keys() else 0
        if cap_n > 0:
            cap_m = int(r["max_auto_retries"] if "max_auto_retries" in r.keys() and r["max_auto_retries"] is not None else 20)
            cap_str = f"  cap×{cap_n}/{cap_m}" if cap_m > 0 else f"  cap×{cap_n}"
    except (TypeError, ValueError, KeyError):
        pass
    progress_str = ""
    if r["status"] == "running":
        if pct is not None:
            try:
                progress_str = f"  {float(pct):.0f}%"
            except (TypeError, ValueError):
                progress_str = ""
        elif last_log:
            clipped = last_log if len(last_log) <= 60 else last_log[:59] + "…"
            progress_str = f"  {clipped}"
    else:
        from taskqueue._fmt import shown_progress

        last_pct = shown_progress(r["status"], pct)
        if last_pct is not None:
            progress_str = f"  {last_pct:.0f}% (last run)"
    task_key = f"{r['project']}/{r['task_id']}{err_str}{off_str}{cap_str}{progress_str}"
    detail = None
    if reason and status in ("waiting", "queued", "stopped"):
        detail = reason
        if err == "tq.block.mutex":
            holders = []
            for lk in (res.get("locks") or []):
                info = occupancy.get(str(lk)) or {}
                if info.get("holder"):
                    holders.append(f"{lk} held by {info['holder']}")
            if holders:
                detail = reason + " — " + "; ".join(holders)
    return status, res_str, task_key, detail, last_log, err


def _list_verbose(rows, occupancy: dict) -> None:
    from taskqueue._fmt import color_status

    desc_w = 40
    print(f"{'PRIO':>5}  {'STATUS':12s}  {'SUITE':16s}  {'TASK':<45s}  DESCRIPTION")
    print("-" * 130)
    for r in rows:
        status, _res_str, task_key, detail, last_log, _err = _list_row_display(r, occupancy)
        suite_val = r["suite"] or ""
        desc = r["description"] or ""
        if len(desc) > desc_w:
            desc = desc[: desc_w - 1] + "…"
        print(f"{r['priority']:>5}  {color_status(f'{status:12s}', status)}  {suite_val:16s}  {task_key:<45s}  {desc}")
        if detail:
            print(f"{'':5}  {'':12s}  {'':16s}  {'':45s}  └─ {detail}")
        # I-22: show last log line for failed tasks
        if status == "failed" and last_log and len(last_log) <= 120:
            print(f"{'':5}  {'':12s}  {'':16s}  {'':45s}  └─ {last_log}")


def _list_compact(rows, occupancy: dict) -> None:
    from taskqueue._fmt import color_status

    print(f"{'PRIO':>5}  {'STATUS':12s}  {'RESOURCES':22s}  TASK")
    print("-" * 75)
    for r in rows:
        status, res_str, task_key, detail, last_log, _err = _list_row_display(r, occupancy)
        worker_str = f"  @{r['last_worker_id']}" if r["last_worker_id"] else ""
        print(f"{r['priority']:>5}  {color_status(f'{status:12s}', status)}  {res_str:22s}  {task_key}{worker_str}")
        if detail:
            print(f"{'':5}  {'':12s}  {'':22s}  └─ {detail}")
        # I-22: show last log line for failed tasks
        if status == "failed" and last_log and len(last_log) <= 120:
            print(f"{'':5}  {'':12s}  {'':22s}  └─ {last_log}")


def cmd_list(
    home: Path,
    project: str | None,
    *,
    tags: list[str] | None = None,
    suite: str | None = None,
    status_filter: str | None = None,
    worker_filter: str | None = None,
    verbose: bool = False,
    as_json: bool = False,
    limit: int | None = None,
    offset: int = 0,
    artifacts: bool = False,
) -> int:
    from taskqueue.db import open_db

    db = _db(home)
    if not db.exists():
        print("No queue DB. Run `tq start`.")
        return 1
    conn = open_db(db, migrate=False)
    rows, occupancy = _list_fetch_rows(
        conn, project,
        tags=tags, suite=suite,
        status_filter=status_filter, worker_filter=worker_filter,
    )
    if offset:
        rows = rows[offset:]
    if limit is not None:
        rows = rows[:limit]
    if not rows:
        if as_json:
            print("[]")
        else:
            print("Queue is empty.")
        conn.close()
        return 0
    if as_json:
        _list_json(rows, occupancy, home, stat_artifacts=artifacts)
    elif verbose:
        _list_verbose(rows, occupancy)
    else:
        _list_compact(rows, occupancy)
    conn.close()
    return 0


def cmd_describe(home: Path, key: str) -> int:
    """Print full details for a single task."""
    from taskqueue.db import open_db

    db = _db(home)
    if not db.exists():
        print("No queue DB.")
        return 1
    project, task_id = _split_key(key)
    conn = open_db(db, migrate=False)
    row = get_task(conn, project, task_id)
    if row is None:
        conn.close()
        print(f"Task not found: {key}")
        return 1
    from taskqueue._db_tasks import run_summary

    keys = row.keys()
    rs = run_summary(conn, project, task_id)
    conn.close()
    print(f"{'─' * 60}")
    print(f"  {project}/{task_id}")
    print(f"{'─' * 60}")
    print(f"  {'run_number':<22s} {rs['run_number']}")
    for k in ("last_run_id", "last_run_started_at", "last_run_finished_at"):
        if rs[k] is not None:
            print(f"  {k:<22s} {rs[k]}")
    for k in keys:
        val = row[k]
        if k in ("args", "resume_args", "requires_paths", "depends_on", "depends_on_any", "tags", "resources", "requires"):
            try:
                val = json.loads(val) if val else val
            except Exception:
                pass
        if val not in (None, "", [], {}):
            print(f"  {k:<22s} {val}")
    return 0



def cmd_stop(home: Path, key: str, *, force: bool = False, reason: str | None = None) -> int:
    from taskqueue._cli_helpers import EXIT_UNREACHABLE, master_mutation

    project, task_id = _split_key(key)
    resp, mode = master_mutation(home, f"/api/stop/{project}/{task_id}", force=force, params={"reason": reason})
    if mode == "unreachable":
        return EXIT_UNREACHABLE
    if mode == "master":
        if not resp.get("ok"):
            print(f"Error: {resp.get('error', resp)}")
            return 1
        print(f"Stop requested for {key}.")
        return 0
    print("[tq] daemon not running; writing SQLite directly (offline repair)", file=sys.stderr)
    from taskqueue.db import get_task, open_db, request_stop_with_control, set_run_stop_reason, set_task_status
    db = open_db(_db(home))
    row = get_task(db, project, task_id)
    if row is None:
        db.close()
        print(f"Error: task {key} not found.")
        return 1
    if _check_ownership_or_print(row, key, force=force):
        db.close()
        return 1
    if reason:
        run_row = db.execute(
            "SELECT run_id FROM runs WHERE project=? AND task_id=? ORDER BY run_id DESC LIMIT 1",
            (project, task_id),
        ).fetchone()
        if run_row:
            set_run_stop_reason(db, run_row["run_id"], reason)
        else:
            print("[tq] warning: --reason not stored (task has no runs yet)", file=sys.stderr)
    # Match HTTP: queued tasks become interrupted immediately (no sticky stop_requested).
    if row and row["status"] == "queued":
        set_task_status(db, project, task_id, "interrupted")
    else:
        op = os.environ.get("TQ_OPERATOR") or ""
        request_stop_with_control(db, home, project, task_id,
                                  requested_by=f"{op + ' ' if op else ''}via cli offline (pid {os.getpid()})")
    db.close()
    print(f"Stop requested for {key}.")
    return 0


def cmd_stop_project(home: Path, project: str) -> int:
    from taskqueue._cli_helpers import _daemon_running
    from taskqueue.db import get_running_tasks, open_db, request_stop_with_control
    from taskqueue.master_client import via_master
    db = open_db(_db(home))
    rows = [r for r in get_running_tasks(db) if r["project"] == project]
    n = 0
    for row in rows:
        resp = via_master(home, "POST", f"/api/stop/{row['project']}/{row['task_id']}")
        if resp is None:
            if _daemon_running(home) is not None:
                db.close()
                print("[ERROR] daemon is running but did not answer; refusing a direct SQLite write.", file=sys.stderr)
                return EXIT_UNREACHABLE
            request_stop_with_control(db, home, row["project"], row["task_id"])
        elif not resp.get("ok"):
            print(f"Error stopping {row['project']}/{row['task_id']}: {resp.get('error', resp)}")
            continue
        n += 1
    db.close()
    print(f"Stop requested for {n} running task(s) in {project}.")
    return 0



# Patchable fields that are mirrored in tasks columns, so a patch can be checked after it is applied.
_PATCH_DB_FIELDS = ("enabled", "priority", "wall_clock_cap_sec", "max_attempts", "description")


def _patch_snapshot(home: Path, project: str, task_id: str) -> dict:
    from taskqueue.db import get_task, open_db

    db = open_db(_db(home), migrate=False)
    try:
        row = get_task(db, project, task_id)
        return {f: row[f] for f in _PATCH_DB_FIELDS if row is not None and f in row.keys()}
    finally:
        db.close()


def _report_patch(key: str, updates: dict, before: dict, after: dict) -> int:
    """Print `field: old -> new` and fail (exit 1) when a mirrored field did not take the new value."""
    lines, failed = [], []
    for k, want in updates.items():
        if k not in after:
            lines.append(f"  {k}: -> {want!r} (YAML only)")
            continue
        want_db = int(want) if k == "enabled" else want
        lines.append(f"  {k}: {before.get(k)!r} -> {after.get(k)!r}")
        if after.get(k) != want_db:
            failed.append(k)
    print(f"Patched {key}:")
    print("\n".join(lines))
    if failed:
        print(f"Error: {', '.join(failed)} did not change in the queue database (check the YAML file "
              f"and `tq validate`).", file=sys.stderr)
        return 1
    return 0


def cmd_patch(home: Path, key: str, pairs: list[str]) -> int:
    from taskqueue.config import load_projects
    from taskqueue.db import get_config, get_task, open_db, parse_task_yaml, upsert_task
    from taskqueue.yaml_patch import patch_task_yaml
    project, task_id = _split_key(key)
    updates: dict = {}
    for p in pairs:
        if "=" not in p:
            print(f"Error: expected k=v, got {p!r}")
            return 1
        k, v = p.split("=", 1)
        if k == "enabled":
            lv = v.strip().lower()
            if lv in ("1", "true", "yes", "on"):
                updates[k] = True
            elif lv in ("0", "false", "no", "off"):
                updates[k] = False
            else:
                print(f"Error: enabled must be true/false, got {v!r}")
                return 1
        elif k in ("priority", "wall_clock_cap_sec", "max_attempts"):
            if not v.lstrip("-").isdigit():
                print(f"Error: {k} must be an integer, got {v!r}")
                return 1
            updates[k] = int(v)
        else:
            updates[k] = v
    db = open_db(_db(home))
    row = get_task(db, project, task_id)
    if row is None or not row["yaml_source"]:
        db.close()
        print(f"Error: {key} not found or has no yaml_source.")
        return 1
    prior_status = str(row["status"] or "")
    db.close()
    before = _patch_snapshot(home, project, task_id)
    from taskqueue.master_client import via_master
    resp = via_master(
        home, "POST", f"/api/tasks/{project}/{task_id}/patch", body={"updates": updates}
    )
    if resp is not None:
        if not resp.get("ok"):
            print(f"Error: {resp.get('error', resp)}")
            return 1
        rc = _report_patch(key, updates, before, _patch_snapshot(home, project, task_id))
        if updates.get("enabled") is True:
            warn_enabled_but_terminal(project, task_id, prior_status)
        return rc
    from taskqueue._cli_helpers import EXIT_UNREACHABLE, _daemon_running

    if _daemon_running(home) is not None:
        print("[ERROR] daemon is running but did not answer; refusing an offline patch.", file=sys.stderr)
        return EXIT_UNREACHABLE
    print("[OFFLINE] daemon not running; patching YAML and SQLite directly (offline repair)", file=sys.stderr)
    db = open_db(_db(home))
    row = get_task(db, project, task_id)
    if row is None or not row["yaml_source"]:
        db.close()
        print(f"Error: {key} not found or has no yaml_source.")
        return 1
    prior_status = str(row["status"] or "")
    path = Path(row["yaml_source"])
    try:
        applied = patch_task_yaml(path, updates, task_id=task_id)
    except ValueError as exc:
        db.close()
        print(f"Error: {exc}")
        return 1
    projects = load_projects(home)
    proj = projects.get(project)
    if proj is None:
        db.close()
        print(f"Error: project {project} not registered.")
        return 1
    upsert_task(db, parse_task_yaml(
        path, proj, allow_remote_notify=bool(get_config(db).get("notify_allow_remote"))
    ))
    db.close()
    rc = _report_patch(key, {k: v for k, v in updates.items() if k in applied or k in _PATCH_DB_FIELDS},
                       before, _patch_snapshot(home, project, task_id))
    if updates.get("enabled") is True:
        warn_enabled_but_terminal(project, task_id, prior_status)
    return rc


def cmd_suspend(home: Path, key: str) -> int:
    """Ask a running task to exit 75 via control file (no SIGTERM)."""
    from taskqueue.db import get_task, open_db, write_control_action
    project, task_id = _split_key(key)
    db = open_db(_db(home))
    row = get_task(db, project, task_id)
    db.close()
    if row is None:
        print(f"Error: Task {key} not found.")
        return 1
    if row["status"] != "running":
        print(f"Error: {key} is not running (status={row['status']}).")
        return 1
    path = write_control_action(home, project, task_id, "suspend")
    print(f"Suspend requested for {key} via {path} (task should exit 75).")
    return 0



def _print_retry_report(result: dict) -> None:
    requeued = result.get("requeued") or []
    skipped = result.get("skipped") or []
    enabled = result.get("enabled") or []
    if enabled:
        print(f"enabled: {', '.join(enabled)}")
    if requeued:
        print(f"requeued: {', '.join(requeued)}")
    else:
        print("requeued: (none)")
    if skipped:
        parts = [f"{s['task_id']} ({s['reason']})" for s in skipped]
        print(f"skipped: {', '.join(parts)}")


def _enable_tasks_for_retry(home: Path, project: str, task_ids: list[str]) -> list[str]:
    """Patch YAML enabled=true + upsert for each task that has yaml_source. Returns enabled ids."""
    from taskqueue.config import load_projects
    from taskqueue.db import get_config, get_task, open_db, parse_task_yaml, upsert_task
    from taskqueue.yaml_patch import patch_task_yaml

    projects = load_projects(home)
    proj = projects.get(project)
    if proj is None:
        return []
    db = open_db(_db(home))
    allow_remote = bool(get_config(db).get("notify_allow_remote"))
    enabled: list[str] = []
    for tid in task_ids:
        row = get_task(db, project, tid)
        if row is None or not row["yaml_source"]:
            continue
        path = Path(row["yaml_source"])
        if not path.is_file():
            continue
        try:
            patch_task_yaml(path, {"enabled": True}, task_id=tid)
            upsert_task(db, parse_task_yaml(path, proj, allow_remote_notify=allow_remote))
            enabled.append(tid)
        except Exception as exc:
            print(f"[WARN] could not enable {project}/{tid}: {exc}", file=sys.stderr)
    db.close()
    return enabled


def _print_retry_wait_hint(project: str, task_ids: "list[str]") -> None:
    from taskqueue.waiting import print_wait_hint

    keys = [t if "/" in t else f"{project}/{t}" for t in task_ids]
    print_wait_hint(keys, project=project)


def cmd_retry(
    home: Path,
    key: str,
    set_args: "list[str] | None" = None,
    *,
    downstream: bool = False,
    enable_downstream: bool = False,
) -> int:
    from taskqueue.db import (
        collect_downstream_task_ids,
        get_task,
        open_db,
        retry_skip_reason,
        retry_task,
        retry_task_with_downstream,
        set_args_override,
    )
    import json as _json

    if enable_downstream:
        downstream = True
    project, task_id = _split_key(key)
    db = open_db(_db(home))
    row = get_task(db, project, task_id)

    if downstream and set_args:
        print("  --downstream cannot be combined with --set")
        db.close()
        return 1
    if enable_downstream and set_args:
        print("  --enable-downstream cannot be combined with --set")
        db.close()
        return 1

    # R2: validate --set overrides against overridable_args allowlist
    base_args: list[str] = []
    if set_args:
        for flag in set_args:
            if not flag.startswith("-") or not flag.split("=")[0].strip("-"):
                print(f"  invalid --set '{flag}': expected --flag or --flag=value")
                db.close()
                return 1
        if row is None:
            print(f"  {key}: task not found; cannot apply --set.")
            db.close()
            return 1
        allowed = []
        if row is not None and "overridable_args" in row.keys():
            try:
                allowed = json_list(row["overridable_args"])
            except Exception:
                pass
        if not allowed:
            print(f"  {key}: overridable_args is empty — no args may be overridden via --set.")
            db.close()
            return 1
        for flag in set_args:
            name = flag.split("=")[0]
            if name not in allowed:
                print(f"  {key}: '{name}' is not in overridable_args ({', '.join(allowed)})")
                db.close()
                return 1

    if set_args and row is not None:
        try:
            base_args = list(json_list(row["args"]))
        except Exception:
            base_args = []
        for override in set_args:
            flag_name = override.split("=")[0]
            replaced = False
            i = 0
            while i < len(base_args):
                if base_args[i] == flag_name:
                    if "=" in override:
                        base_args[i] = override
                        if i + 1 < len(base_args) and not base_args[i + 1].startswith("-"):
                            base_args.pop(i + 1)
                    replaced = True
                    break
                elif base_args[i].startswith(flag_name + "="):
                    base_args[i] = override
                    replaced = True
                    break
                i += 1
            if not replaced:
                base_args.append(override)

    chain_ids = [task_id]
    if downstream:
        chain_ids = [task_id] + collect_downstream_task_ids(db, project, task_id)
    db.close()

    enabled_ids: list[str] = []
    if enable_downstream:
        enabled_ids = _enable_tasks_for_retry(home, project, chain_ids)

    from taskqueue.master_client import via_master
    body: dict = {}
    if set_args and row is not None:
        body["args_override"] = base_args
    if downstream:
        body["downstream"] = True
    if enable_downstream:
        body["enable_downstream"] = True
    resp = via_master(
        home, "POST", f"/api/tasks/{project}/{task_id}/retry", body=body or None
    )
    if resp is not None:
        ok = bool(resp.get("ok"))
        if downstream:
            result = {
                "requeued": resp.get("requeued") or [],
                "skipped": resp.get("skipped") or [],
                "enabled": resp.get("enabled") or enabled_ids,
            }
            _print_retry_report(result)
            if task_id in result["requeued"]:
                _print_retry_wait_hint(project, result["requeued"])
                return 0
            print(resp.get("error") or f"{key} not in a retryable state.")
            return 1
        if ok:
            if downstream:
                requeued = resp.get("requeued") or [task_id]
                print(f"Re-queued {len(requeued)} task(s): {', '.join(requeued)}")
            elif set_args:
                print(f"{key} re-queued with arg overrides: {' '.join(set_args)}")
            else:
                print(f"{key} re-queued.")
            _print_retry_wait_hint(project, resp.get("requeued") or [task_id])
            return 0
        print(resp.get("error") or f"{key} not in a retryable state.")
        return 1

    from taskqueue._cli_helpers import _daemon_running

    if _daemon_running(home) is not None:
        print("[ERROR] daemon is running but did not answer; refusing a direct SQLite write.", file=sys.stderr)
        return EXIT_UNREACHABLE
    print("[OFFLINE] daemon unreachable; writing SQLite directly (offline repair)", file=sys.stderr)
    db = open_db(_db(home))
    if downstream:
        result = retry_task_with_downstream(db, project, task_id, home=home)
        result["enabled"] = enabled_ids
        db.close()
        _print_retry_report(result)
        if task_id not in result["requeued"]:
            print(f"{key} not in a retryable state (and no retryable downstream).")
            return 1
        _print_retry_wait_hint(project, result["requeued"])
        return 0
    ok = retry_task(db, project, task_id, home=home)
    # F-086: write override only after a successful retry to avoid stale override on failure.
    if ok and set_args and row is not None:
        set_args_override(db, project, task_id, base_args)
    reason = None if ok else retry_skip_reason(db, project, task_id)
    db.close()
    if ok:
        if set_args:
            print(f"{key} re-queued with arg overrides: {' '.join(set_args)}")
        else:
            print(f"{key} re-queued.")
        _print_retry_wait_hint(project, [task_id])
        return 0
    if reason == "max_attempts":
        print(
            f"{key} has exhausted its attempt cap. "
            f"Use 'tq patch {key} max_attempts=0' to remove the cap."
        )
        return 1
    if reason == "disabled":
        print(f"{key} is disabled (enabled=0); enable it or use --enable-downstream.")
        return 1
    print(
        f"{key} not in a retryable state"
        + (f" ({reason})" if reason else "")
        + "."
    )
    return 1


def cmd_reverify(home: Path, root_key: str, up_to: str) -> int:
    """Enable root→up_to, disable other downstream of root, retry --downstream."""
    from taskqueue.config import load_projects
    from taskqueue.db import (
        collect_ancestors_to_root,
        collect_downstream_task_ids,
        get_config,
        get_task,
        open_db,
        parse_task_yaml,
        retry_task_with_downstream,
        upsert_task,
    )
    from taskqueue.master_client import via_master
    from taskqueue.yaml_patch import patch_task_yaml

    project, root_id = _split_key(root_key)
    up_id = up_to.split("/", 1)[-1] if "/" in up_to else up_to

    if via_master(home, "GET", "/api/status") is None:
        print("[OFFLINE] daemon unreachable; refusing reverify", file=sys.stderr)
        return EXIT_UNREACHABLE

    db = open_db(_db(home))
    keep = collect_ancestors_to_root(db, project, up_id, root=root_id)
    if keep is None:
        db.close()
        print(f"Error: {up_id} is not downstream of {root_key}")
        return 1
    cone = {root_id} | set(collect_downstream_task_ids(db, project, root_id))
    disable = sorted(cone - keep)
    enable = sorted(keep)
    projects = load_projects(home)
    proj = projects.get(project)
    if proj is None:
        db.close()
        print(f"Error: project {project} not registered.")
        return 1
    allow_remote = bool(get_config(db).get("notify_allow_remote"))

    def _patch_enabled(tid: str, value: bool) -> None:
        row = get_task(db, project, tid)
        if row is None or not row["yaml_source"]:
            return
        path = Path(row["yaml_source"])
        if not path.is_file():
            return
        patch_task_yaml(path, {"enabled": value}, task_id=tid)
        upsert_task(db, parse_task_yaml(path, proj, allow_remote_notify=allow_remote))

    for tid in disable:
        _patch_enabled(tid, False)
    for tid in enable:
        _patch_enabled(tid, True)
    if disable:
        print(f"disabled: {', '.join(disable)}")
    if enable:
        print(f"enabled: {', '.join(enable)}")
    result = retry_task_with_downstream(db, project, root_id)
    _print_retry_report(result)
    print("status:")
    for tid in sorted(cone):
        row = get_task(db, project, tid)
        st = row["status"] if row else "?"
        en = int(row["enabled"]) if row else 0
        print(f"  {project}/{tid}: {st} enabled={en}")
    db.close()
    if root_id not in result["requeued"]:
        return 1
    return 0


def cmd_locks(home: Path, *, as_json: bool = False) -> int:
    from taskqueue.db import lock_occupancy, open_db

    db = _db(home)
    if not db.exists():
        print("No queue DB.")
        return 1
    conn = open_db(db, migrate=False)
    occ = lock_occupancy(conn)
    conn.close()
    if as_json:
        emit_json(occ, ensure_ascii=False)
        return 0
    if not occ:
        print("No active locks.")
        return 0
    print(f"{'LOCK':<32s}  {'HOLDER':<28s}  WAITERS")
    print("-" * 80)
    for name, info in sorted(occ.items()):
        holder = info.get("holder") or "(none)"
        waiters = ", ".join(info.get("waiters") or []) or "-"
        print(f"{name:<32s}  {holder:<28s}  {waiters}")
    return 0


def cmd_force(home: Path, key: str) -> int:
    from taskqueue._cli_helpers import EXIT_UNREACHABLE, master_mutation

    project, task_id = _split_key(key)
    resp, mode = master_mutation(home, f"/api/force/{project}/{task_id}")
    if mode == "unreachable":
        return EXIT_UNREACHABLE
    if mode == "master":
        err = None if resp.get("ok") else (resp.get("error") or "failed")
    else:
        from taskqueue.db import force_task, open_db
        db = open_db(_db(home))
        err = force_task(db, project, task_id)
        db.close()
    if err:
        print(f"Error: {err}")
        return 1
    print(f"{key} marked to force run on next scheduler tick.")
    return 0


def cmd_remove(home: Path, key: str, *, force: bool = False) -> int:
    from taskqueue._cli_helpers import EXIT_UNREACHABLE, master_mutation

    project, task_id = _split_key(key)
    resp, mode = master_mutation(home, f"/api/remove/{project}/{task_id}", force=force)
    if mode == "unreachable":
        return EXIT_UNREACHABLE
    if mode == "master":
        err = None if resp.get("ok") else (resp.get("error") or "failed")
    else:
        from taskqueue.db import get_task, open_db, remove_task
        db = open_db(_db(home))
        row = get_task(db, project, task_id)
        if _check_ownership_or_print(row, key, force=force):
            db.close()
            return 1
        err = remove_task(db, project, task_id)
        db.close()
    if err:
        print(f"Error: {err}")
        return 1
    print(f"Removed task {key} from database.")
    return 0


def cmd_remove_project(home: Path, project: str, *, force: bool = False) -> int:
    """Delete all tasks and runs for a project from the DB (does not touch projects.yaml)."""
    from taskqueue._cli_helpers import EXIT_UNREACHABLE, master_mutation

    db_path = _db(home)
    if not db_path.exists():
        print("No queue DB.")
        return 1
    resp, mode = master_mutation(home, f"/api/project/remove/{project}", force=force)
    if mode == "unreachable":
        return EXIT_UNREACHABLE
    if mode == "master":
        if not resp.get("ok"):
            print(f"Error: {resp.get('error', 'failed')}")
            return 1
        n_tasks, n_runs = int(resp.get("tasks", 0)), int(resp.get("runs", 0))
    else:
        from taskqueue.db import open_db

        # Safety: refuse if any task is currently running unless --force
        conn = open_db(db_path)
        running = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE project = ? AND status = 'running'",
            (project,),
        ).fetchone()[0]
        if running and not force:
            conn.close()
            print(
                f"Error: {running} task(s) are running in project '{project}'. "
                "Stop them first or use --force."
            )
            return 1
        n_tasks = conn.execute("DELETE FROM tasks WHERE project = ?", (project,)).rowcount
        conn.execute(
            "DELETE FROM resource_history WHERE run_id IN (SELECT run_id FROM runs WHERE project = ?)",
            (project,),
        )
        n_runs = conn.execute("DELETE FROM runs WHERE project = ?", (project,)).rowcount
        conn.commit()
        conn.close()
    if n_tasks == 0 and n_runs == 0:
        print(f"No tasks found for project '{project}'.")
        return 1
    print(f"Removed project '{project}': {n_tasks} task(s), {n_runs} run(s) deleted from DB.")
    print("Note: also remove the project from projects.yaml to prevent re-sync.")
    return 0


def cmd_logs(home: Path, key: str, *, max_bytes: int | None = None) -> int:
    return cmd_logs_extended(home, key, max_bytes=max_bytes, grep=None, jsonl=False)



def cmd_diagnose(home: Path, key: str) -> int:
    from taskqueue.db import open_db
    from taskqueue.diagnose import build_diagnose

    if "/" not in key:
        print("Usage: tq diagnose <project/task_id>")
        return 1
    project, task_id = key.split("/", 1)
    db = _db(home)
    if not db.exists():
        print("No queue DB.")
        return 1
    conn = open_db(db)
    try:
        bundle = build_diagnose(conn, project, task_id, home=home)
    finally:
        conn.close()
    if not bundle["task"]:
        print(f"Unknown task {project}/{task_id}")
        return 1
    t = bundle["task"]
    print(f"{project}/{task_id}  status={t['status']}  error={t.get('last_error_code') or '-'}")
    if bundle["error_description"]:
        print(f"  {bundle['error_description']}")
    if t.get("block_reason"):
        print(f"  block_reason: {t['block_reason']}")
    print(f"runs: {len(bundle['runs'])}")
    last = bundle["last_run"]
    if last:
        print(
            f"last_run: id={last['run_id']} status={last['status']} "
            f"exit={last['exit_code']} worker={last.get('worker_id') or 'local'}"
        )
        print(f"log: {last.get('log_path')}")
    m = bundle.get("metrics") or {}
    res = (m.get("resources") or {})
    cur, peak, avg = res.get("current") or {}, res.get("peak") or {}, res.get("avg") or {}
    from taskqueue.log_issues import format_log_counts
    log_s = format_log_counts(m.get("log") or (m.get("metrics") or {}).get("log"))
    if any(v is not None for v in list(cur.values()) + list(peak.values()) + list(avg.values())):
        parts = []
        if cur.get("cpu_pct") is not None:
            parts.append(f"cpu={cur['cpu_pct']}%")
        if cur.get("ram_gib") is not None:
            parts.append(f"ram={cur['ram_gib']}G")
        if cur.get("vram_gib") is not None:
            parts.append(f"vram={cur['vram_gib']}G")
        if avg.get("gpu_util_pct") is not None:
            parts.append(f"gpu_avg={avg['gpu_util_pct']}%")
        elif cur.get("gpu_util_pct") is not None:
            parts.append(f"gpu={cur['gpu_util_pct']}%")
        pk = []
        if peak.get("ram_gib") is not None:
            pk.append(f"ram={peak['ram_gib']}G")
        if peak.get("vram_gib") is not None:
            pk.append(f"vram={peak['vram_gib']}G")
        if peak.get("cpu_pct") is not None:
            pk.append(f"cpu={peak['cpu_pct']}%")
        line = "metrics: " + (" ".join(parts) if parts else "—")
        if pk:
            line += f"  peak[{' '.join(pk)}]"
        if log_s:
            line += f"  log: {log_s}"
        line += f"  (source={m.get('source', 'db')})"
        print(line)
    elif log_s:
        print(f"  log: {log_s}")
    if bundle["worker"]:
        w = bundle["worker"]
        print(f"worker: {w.get('worker_id')} status={w.get('status')} last_seen={w.get('last_seen')}")
    if bundle["log_tail"]:
        print("--- log_tail ---")
        for line in bundle["log_tail"][-40:]:
            print(line)
    return 0


def cmd_metrics(home: Path, key: str, *, as_json: bool = False, samples: int = 60) -> int:
    """Print the same run metrics payload as the UI / MCP progress_snapshot."""
    from taskqueue.db import open_db
    from taskqueue.metrics_snapshot import build_run_metrics

    if "/" not in key:
        print("Usage: tq metrics <project/task_id> [--json] [--samples N]")
        return 1
    project, task_id = key.split("/", 1)
    db = _db(home)
    if not db.exists():
        print("No queue DB.")
        return 1
    conn = open_db(db, migrate=False)
    try:
        m = build_run_metrics(
            conn, project, task_id, home=home, samples=samples, include_history=as_json,
        )
    finally:
        conn.close()
    if as_json:
        emit_json(m, ensure_ascii=False)
        return 0
    print(f"{project}/{task_id}  #{m.get('run_id') or '—'}  status={m.get('status')}")
    if m.get("progress_pct") is not None:
        eta = f"  eta={m['eta_sec']}s" if m.get("eta_sec") is not None else ""
        print(f"  progress={m['progress_pct']}%  elapsed={m.get('elapsed_sec')}s{eta}")
    res = m.get("resources") or {}
    cur, peak, avg = res.get("current") or {}, res.get("peak") or {}, res.get("avg") or {}
    print(
        f"  current: cpu={cur.get('cpu_pct')}% ram={cur.get('ram_gib')}G "
        f"vram={cur.get('vram_gib')}G gpu={cur.get('gpu_util_pct')}%"
    )
    print(
        f"  peak:    cpu={peak.get('cpu_pct')}% ram={peak.get('ram_gib')}G "
        f"vram={peak.get('vram_gib')}G"
    )
    if avg.get("gpu_util_pct") is not None:
        print(f"  gpu_avg: {avg['gpu_util_pct']}%")
    from taskqueue.log_issues import format_log_counts
    log_s = format_log_counts(m.get("log") or (m.get("metrics") or {}).get("log"))
    if log_s:
        print(f"  log: {log_s}")
    hs = m.get("history_summary") or {}
    print(f"  samples: {hs.get('n', 0)}  source={m.get('source')}")
    if m.get("last_line"):
        print(f"  last: {m['last_line'][:120]}")
    return 0



def cmd_deps(home: Path, key: str, *, mermaid: bool = False) -> int:
    """I-11: Show dependency graph for a project or task."""
    from taskqueue.db import open_db

    db = _db(home)
    if not db.exists():
        print("No queue DB.")
        return 1

    conn = open_db(db, migrate=False)
    if "/" in key:
        project, task_id = key.split("/", 1)
        rows = conn.execute(
            "SELECT task_id, depends_on, depends_on_any, status FROM tasks WHERE project=?", (project,)
        ).fetchall()
    else:
        project = key
        task_id = None
        rows = conn.execute(
            "SELECT task_id, depends_on, depends_on_any, status FROM tasks WHERE project=?", (project,)
        ).fetchall()
    conn.close()

    if not rows:
        print(f"No tasks found for project '{project}'.")
        return 1

    # Build dependency map
    task_map: dict[str, dict] = {}
    for r in rows:
        deps = json_list(r["depends_on"])
        deps_any = json_list(r["depends_on_any"]) if "depends_on_any" in r.keys() else []
        task_map[r["task_id"]] = {"deps": deps, "deps_any": deps_any, "status": r["status"]}

    if mermaid:
        print("graph TD")
        for tid, info in task_map.items():
            print(f"  {tid}[{tid}]:::s_{info['status']}")
            for dep in info["deps"]:
                print(f"  {dep} --> {tid}")
            for dep in info["deps_any"]:
                print(f"  {dep} -.->|OR| {tid}")
        print("  classDef s_completed fill:green,color:white")
        print("  classDef s_failed fill:red,color:white")
        print("  classDef s_running fill:yellow")
        print("  classDef s_queued fill:lightblue")
        return 0

    if task_id:
        # Show what this task depends on and what depends on it
        info = task_map.get(task_id)
        if not info:
            print(f"Task '{task_id}' not found in project '{project}'.")
            return 1
        print(f"{project}/{task_id}  [{info['status']}]")
        print("  depends on (AND):")
        for dep in info["deps"]:
            dep_status = task_map.get(dep, {}).get("status", "?")
            print(f"    {dep}  [{dep_status}]")
        if info["deps_any"]:
            print("  depends on any (OR):")
            for dep in info["deps_any"]:
                dep_status = task_map.get(dep, {}).get("status", "?")
                print(f"    {dep}  [{dep_status}]")
        print("  depended on by:")
        for tid, tinfo in task_map.items():
            if task_id in tinfo["deps"] or task_id in tinfo["deps_any"]:
                or_label = " (OR)" if task_id in tinfo["deps_any"] else ""
                print(f"    {tid}  [{tinfo['status']}]{or_label}")
    else:
        # Show all tasks with deps
        print(f"Project: {project}")
        for tid, info in task_map.items():
            print(f"  {tid}  [{info['status']}]")
            for dep in info["deps"]:
                dep_status = task_map.get(dep, {}).get("status", "?")
                print(f"    |-- {dep}  [{dep_status}]")
            for dep in info["deps_any"]:
                dep_status = task_map.get(dep, {}).get("status", "?")
                print(f"    |-- {dep}  [{dep_status}] (OR)")
    return 0



def cmd_logs_extended(
    home: Path,
    key: str,
    *,
    max_bytes: int | None = None,
    grep: str | None = None,
    jsonl: bool = False,
) -> int:
    """I-13: cmd_logs with --grep and --jsonl support."""
    from taskqueue.db import open_db
    from taskqueue.logio import (
        DEFAULT_LOG_TAIL_BYTES,
        clamp_tail_bytes,
        read_log_tail,
    )

    project, task_id = _split_key(key)
    db = open_db(_db(home))
    row = db.execute(
        "SELECT log_path FROM runs WHERE project=? AND task_id=? ORDER BY run_id DESC LIMIT 1",
        (project, task_id),
    ).fetchone()
    db.close()
    if not row or not row["log_path"]:
        print(f"No log found for {key}.")
        return 1
    log = Path(row["log_path"])
    if not log.exists():
        print(f"Log file missing: {log}")
        return 1
    try:
        tail = read_log_tail(
            log,
            max_bytes=clamp_tail_bytes(max_bytes, default=DEFAULT_LOG_TAIL_BYTES),
        )
    except OSError as exc:
        print(f"Log read error: {exc}")
        return 1

    lines = tail.text.splitlines(keepends=True)
    if grep:
        pattern_lower = grep.lower()
        lines = [ln for ln in lines if pattern_lower in ln.lower()]
    if jsonl:
        for line in lines:
            stripped = line.rstrip("\n")
            try:
                obj = json.loads(stripped)
                emit_json(obj)
            except Exception:
                print(stripped)
    else:
        text = "".join(lines)
        print(text, end="" if text.endswith("\n") else "\n")
    return 0



def follow_log(
    home: Path,
    key: str,
    *,
    out=None,
    sleep=None,
    poll_sec: float = 1.0,
    tail_bytes: int | None = None,
) -> int:
    """``tq logs -f``: print the tail, then new output until the task settles.

    Exit codes match ``tq wait``: 0 completed, 1 failed / interrupted / stopped, 3 task or log
    not found. Ctrl+C exits 0. A final read after the status flips avoids losing the last lines.
    """
    import time

    from taskqueue.db import open_db
    from taskqueue.logio import DEFAULT_LOG_TAIL_BYTES, clamp_tail_bytes, read_log_tail
    from taskqueue.waiting import EXIT_FAILED, EXIT_NOT_FOUND, EXIT_OK, SETTLED_STATUSES

    out = out or sys.stdout
    sleep = sleep or time.sleep
    project, task_id = _split_key(key)
    dbp = _db(home)
    if not dbp.exists():
        print("No queue DB.", file=sys.stderr)
        return EXIT_NOT_FOUND

    def _state() -> tuple[str | None, str | None]:
        conn = open_db(dbp, migrate=False)
        try:
            t = get_task_status(conn, project, task_id)
            r = conn.execute(
                "SELECT log_path FROM runs WHERE project=? AND task_id=? ORDER BY run_id DESC LIMIT 1",
                (project, task_id),
            ).fetchone()
        finally:
            conn.close()
        return (str(t["status"]) if t else None), (r["log_path"] if r else None)

    status, log_path = _state()
    if status is None:
        print(f"Unknown task {key}", file=sys.stderr)
        return EXIT_NOT_FOUND
    offset = 0
    cur_log: Path | None = None
    try:
        while True:
            if log_path and (cur_log is None or str(cur_log) != str(log_path)):
                cur_log, offset = Path(log_path), 0
                if cur_log.exists():
                    tail = read_log_tail(
                        cur_log, max_bytes=clamp_tail_bytes(tail_bytes, default=DEFAULT_LOG_TAIL_BYTES),
                    )
                    out.write(tail.text)
                    offset = cur_log.stat().st_size
            elif cur_log is not None and cur_log.exists():
                size = cur_log.stat().st_size
                if size < offset:
                    offset = 0
                if size > offset:
                    with cur_log.open("rb") as fh:
                        fh.seek(offset)
                        out.write(fh.read(size - offset).decode("utf-8", "replace"))
                    offset = size
            out.flush()
            if status in SETTLED_STATUSES:
                return EXIT_OK if status == "completed" else EXIT_FAILED
            sleep(poll_sec)
            status, log_path = _state()
            if status is None:
                return EXIT_NOT_FOUND
    except KeyboardInterrupt:
        return EXIT_OK


def cmd_result(home: Path, key: str) -> int:
    """Print the last non-empty line of the latest log; exit 0 iff status=completed."""
    from taskqueue.db import open_db

    project, task_id = _split_key(key)
    db_path = _db(home)
    if not db_path.exists():
        print("No queue DB.", file=sys.stderr)
        return 1
    conn = open_db(db_path, migrate=False)
    task = get_task_status(conn, project, task_id)
    run = conn.execute(
        "SELECT log_path FROM runs WHERE project=? AND task_id=? ORDER BY run_id DESC LIMIT 1",
        (project, task_id),
    ).fetchone()
    conn.close()
    if task is None:
        print(f"Task not found: {key}", file=sys.stderr)
        return 1
    if not run or not run["log_path"]:
        print(f"No log found for {key}.", file=sys.stderr)
        return 1
    log = Path(run["log_path"])
    if not log.exists():
        print(f"Log file missing: {log}", file=sys.stderr)
        return 1
    try:
        line = last_nonempty_line(log)
    except OSError as exc:
        print(f"Log read error: {exc}", file=sys.stderr)
        return 1
    if line is None:
        print(f"Log empty for {key}.", file=sys.stderr)
        return 1
    print(line)
    return 0 if task["status"] == "completed" else 1


def cmd_tail(home: Path, key: str, *, last_n: int | None = None) -> int:
    """Follow a task log, or dump the last N lines and exit when ``last_n`` is set."""
    import time
    from taskqueue.db import open_db

    project, task_id = _split_key(key)
    db_path = _db(home)
    if not db_path.exists():
        print("No queue DB.")
        return 1

    conn = open_db(db_path)
    row = conn.execute(
        "SELECT log_path FROM runs WHERE project=? AND task_id=? ORDER BY run_id DESC LIMIT 1",
        (project, task_id),
    ).fetchone()
    conn.close()

    if not row or not row["log_path"]:
        print(f"No log found for {key}.")
        return 1
    log = Path(row["log_path"])
    if not log.exists():
        print(f"Log file missing: {log}")
        return 1

    if last_n is not None:
        try:
            lines = read_last_lines(log, last_n)
        except OSError as exc:
            print(f"Log read error: {exc}")
            return 1
        for line in lines:
            print(line)
        return 0

    print(f"Tailing {log}  (Ctrl+C to stop)")
    try:
        with open(log, "r", encoding="utf-8", errors="replace") as fh:
            fh.seek(0, 2)  # seek to end
            check_counter = 0
            while True:
                line = fh.readline()
                if line:
                    print(line, end="")
                else:
                    time.sleep(0.25)
                    check_counter += 1
                    if check_counter >= 8:  # every ~2s
                        check_counter = 0
                        conn2 = open_db(db_path)
                        try:
                            task_row = get_task_status(conn2, project, task_id)
                        finally:
                            conn2.close()
                        if task_row and task_row["status"] not in ("running",):
                            # Drain remaining lines
                            for remaining in fh:
                                print(remaining, end="")
                            print(f"\n[task {key} finished: {task_row['status']}]")
                            return 0
    except KeyboardInterrupt:
        print("\n[tail interrupted]")
        return 0
    return 0



def cmd_tag(home: Path, action: str, key: str, tags_list: list[str]) -> int:
    """I-24: Tag management: add/remove/list tags on a task."""
    from taskqueue.db import open_db

    db_path = _db(home)
    if not db_path.exists():
        print("No queue DB.")
        return 1
    project, task_id = _split_key(key)
    conn = open_db(db_path)
    row = conn.execute(
        "SELECT tags FROM tasks WHERE project=? AND task_id=?", (project, task_id)
    ).fetchone()
    if row is None:
        print(f"Task not found: {key}")
        conn.close()
        return 1

    current_tags: list[str] = json_list(row["tags"])

    if action == "list":
        print(f"Tags for {key}: {current_tags}")
        conn.close()
        return 0
    elif action == "add":
        for t in tags_list:
            if t not in current_tags:
                current_tags.append(t)
        conn.execute(
            "UPDATE tasks SET tags=? WHERE project=? AND task_id=?",
            (json.dumps(current_tags), project, task_id),
        )
        conn.commit()
        print(f"Tags for {key}: {current_tags}")
    elif action == "remove":
        current_tags = [t for t in current_tags if t not in tags_list]
        conn.execute(
            "UPDATE tasks SET tags=? WHERE project=? AND task_id=?",
            (json.dumps(current_tags), project, task_id),
        )
        conn.commit()
        print(f"Tags for {key}: {current_tags}")
    else:
        print(f"Unknown tag action '{action}'. Use: add, remove, list")
        conn.close()
        return 1

    conn.close()
    return 0



def cmd_watch(home: Path, *, interval: float = 2.0) -> int:
    """Live refreshing terminal dashboard. Ctrl+C to exit."""
    import time

    db = _db(home)
    if not db.exists():
        print("No queue DB. Run `tq start`.")
        return 1

    from taskqueue.db import open_db

    def _fmt_elapsed(sec: float) -> str:
        sec = int(sec)
        h, r = divmod(sec, 3600)
        m, s = divmod(r, 60)
        if h:
            return f"{h}h{m:02d}m"
        if m:
            return f"{m}m{s:02d}s"
        return f"{s}s"

    def _eta_str(predicted_sec, started_at_iso: str | None) -> str:
        """Return ' ETA ~Xs' when a predicted duration and start time are available."""
        if not predicted_sec or not started_at_iso:
            return ""
        try:
            from datetime import datetime, timezone
            started = _parse_iso(started_at_iso)
            elapsed = (datetime.now(timezone.utc) - started).total_seconds()
            remaining = float(predicted_sec) - elapsed
            if remaining > 0:
                return f" ETA ~{_fmt_elapsed(remaining)}"
        except Exception:
            pass
        return ""

    def _render_plain(conn) -> str:
        running = conn.execute(
            "SELECT t.*, r.started_at FROM tasks t "
            "LEFT JOIN runs r ON r.project=t.project AND r.task_id=t.task_id AND r.status='running' "
            "WHERE t.status='running'"
        ).fetchall()
        queued_n = conn.execute("SELECT COUNT(*) AS n FROM tasks WHERE status IN ('queued','stopped') AND enabled=1").fetchone()["n"]
        counts = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status").fetchall()}
        ts = time.strftime("%H:%M:%S")
        lines = [
            f"\033[1m tq watch\033[0m  {ts}   "
            f"running: \033[32m{len(running)}\033[0m  queued: {queued_n}  "
            f"completed: {counts.get('completed',0)}  failed: \033[31m{counts.get('failed',0)}\033[0m",
            "─" * 78,
        ]
        if not running:
            lines.append("  (no tasks running)")
        for row in running:
            res = json.loads(row["resources"] or "{}")
            res_str = f"cpu={res.get('cpu_cores',1)} ram={res.get('ram_gib',0):.0f}G"
            desc = (row["description"] or "")[:50]
            eta = _eta_str(row["predicted_duration_sec"], row["started_at"])
            lines.append(f"  \033[32m●\033[0m {row['project']}/{row['task_id']}  {res_str}  {desc}{eta}")
        if queued_n:
            lines.append("")
            queued = conn.execute(
                "SELECT project, task_id FROM tasks WHERE status IN ('queued','stopped') AND enabled=1 "
                "ORDER BY priority DESC, enqueued_at ASC LIMIT 6"
            ).fetchall()
            lines.append("  Queued:")
            for row in queued[:5]:
                lines.append(f"    {row['project']}/{row['task_id']}")
            if queued_n > 5:
                lines.append(f"    … and {queued_n-5} more")
        return "\n".join(lines)

    try:
        from rich.live import Live
        from rich.table import Table
        from rich.panel import Panel
        from rich.console import Console

        def _render_rich(conn):
            running = conn.execute(
                "SELECT t.*, r.started_at FROM tasks t "
                "LEFT JOIN runs r ON r.project=t.project AND r.task_id=t.task_id AND r.status='running' "
                "WHERE t.status='running'"
            ).fetchall()
            queued_n = conn.execute("SELECT COUNT(*) AS n FROM tasks WHERE status IN ('queued','stopped') AND enabled=1").fetchone()["n"]
            counts = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status").fetchall()}
            tbl = Table(title=f"tq watch  {time.strftime('%H:%M:%S')}", expand=True, header_style="bold")
            tbl.add_column("Status", width=9)
            tbl.add_column("Task", min_width=30)
            tbl.add_column("Resources", width=22)
            tbl.add_column("Description")
            for row in running:
                res = json.loads(row["resources"] or "{}")
                res_str = f"cpu={res.get('cpu_cores',1)} ram={res.get('ram_gib',0):.0f}G"
                eta = _eta_str(row["predicted_duration_sec"], row["started_at"])
                desc = ((row["description"] or "")[:58] + eta) if eta else (row["description"] or "")[:60]
                tbl.add_row("[green]running[/green]", f"{row['project']}/{row['task_id']}", res_str, desc)
            queued_rows = conn.execute(
                "SELECT project, task_id, resources, description FROM tasks "
                "WHERE status IN ('queued','stopped') AND enabled=1 "
                "ORDER BY priority DESC, enqueued_at ASC LIMIT 8"
            ).fetchall()
            for row in queued_rows:
                res = json.loads(row["resources"] or "{}")
                res_str = f"cpu={res.get('cpu_cores',1)} ram={res.get('ram_gib',0):.0f}G"
                tbl.add_row("[yellow]queued[/yellow]", f"{row['project']}/{row['task_id']}", res_str, (row["description"] or "")[:60])
            footer = (
                f"running={len(running)}  queued={queued_n}  "
                f"completed={counts.get('completed',0)}  failed={counts.get('failed',0)}"
            )
            return Panel(tbl, subtitle=footer)

        console = Console()
        try:
            with Live(console=console, refresh_per_second=1, screen=True) as live:
                while True:
                    conn = open_db(db)
                    try:
                        live.update(_render_rich(conn))
                    finally:
                        conn.close()
                    time.sleep(interval)
        except KeyboardInterrupt:
            pass
        return 0
    except ImportError:
        pass

    CLEAR = "\033[2J\033[H"
    try:
        while True:
            conn = open_db(db)
            try:
                text = _render_plain(conn)
            finally:
                conn.close()
            print(CLEAR + text, end="", flush=True)
            time.sleep(interval)
    except KeyboardInterrupt:
        print()
    return 0



def cmd_group(home: Path, action: str, project: str, group_name: str) -> int:
    """W6.2: Group-level ops (list, stop, retry)."""
    from taskqueue.db import open_db, request_stop_with_control, retry_task

    db_path = _db(home)
    if not db_path.exists():
        print("No queue DB.")
        return 1
    conn = open_db(db_path)
    rows = conn.execute(
        "SELECT project, task_id, status FROM tasks WHERE project = ? AND grp = ?",
        (project, group_name),
    ).fetchall()
    if not rows:
        print(f"No tasks in group '{group_name}' of project '{project}'.")
        conn.close()
        return 0
    if action == "list":
        print(f"Group '{group_name}' in project '{project}' ({len(rows)} tasks):")
        for r in rows:
            print(f"  {r['task_id']:40s}  {r['status']}")
    elif action == "stop":
        n = 0
        for r in rows:
            if r["status"] == "running":
                request_stop_with_control(conn, home, r["project"], r["task_id"])
                n += 1
        print(f"Stop requested for {n} running task(s) in group '{group_name}'.")
    elif action == "retry":
        n = 0
        for r in rows:
            if retry_task(conn, r["project"], r["task_id"]):
                n += 1
        print(f"Re-queued {n} task(s) in group '{group_name}'.")
    else:
        print(f"Unknown group action '{action}'. Use: list, stop, retry")
        conn.close()
        return 1
    conn.close()
    return 0



def cmd_wait(
    home: Path,
    keys: list[str],
    *,
    timeout_sec: float | None = None,
    mode: str = "all",
    selectors: dict | None = None,
    as_json: bool = False,
    raw_exit: bool = False,
    poll_sec: float = 5.0,
) -> int:
    """Block until selected tasks settle.

    Exit codes: 0 all completed, 1 a task failed/stopped/etc., 2 timeout,
    3 task not found. ``raw_exit`` restores the legacy single-task behaviour
    (task's own exit code, 124 on timeout, 1 on not found).
    """
    import json as _json

    from taskqueue.db import open_db
    from taskqueue.waiting import exit_code_for, resolve_selectors, wait_for

    db_path = _db(home)
    if not db_path.exists():
        print("No queue DB.", file=sys.stderr)
        return 3
    try:
        conn = open_db(db_path, migrate=False)
        try:
            resolved = resolve_selectors(conn, keys=keys, **(selectors or {}))
        finally:
            conn.close()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 3
    if not resolved:
        print("Nothing to wait for (selectors matched no unfinished tasks).", file=sys.stderr)
        return 0
    result = wait_for(
        lambda: open_db(db_path, migrate=False), resolved, mode=mode,
        timeout_sec=timeout_sec, poll_sec=poll_sec,
    )
    if as_json:
        emit_json(result)
    else:
        for t in result["tasks"]:
            if t["settled"]:
                ec = "" if t["exit_code"] is None else f" exit={t['exit_code']}"
                wall = "" if t["wall_sec"] is None else f" {t['wall_sec']}s"
                err = f" [{t['last_error_code']}]" if t["last_error_code"] else ""
                print(f"{t['key']}: {t['status']}{ec}{wall}{err}")
        for k in result["pending"]:
            print(f"{k}: still pending", file=sys.stderr)
        for k in result["not_found"]:
            print(f"Task not found: {k}", file=sys.stderr)
        if result["timed_out"]:
            print(f"Timeout after {result['waited_sec']}s", file=sys.stderr)
    if raw_exit and len(resolved) == 1:
        if result["timed_out"]:
            return 124
        t = result["tasks"][0] if result["tasks"] else None
        if t is None:
            return 1
        if t["exit_code"] is not None:
            return int(t["exit_code"])
        return 0 if t["status"] == "completed" else 1
    return exit_code_for(result)
