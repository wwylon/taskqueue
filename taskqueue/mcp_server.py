"""Minimal stdio MCP server for taskqueue (no extra dependency).

Implements initialize / tools/list / tools/call over JSON-RPC 2.0 newline messages.
"""

from __future__ import annotations
from taskqueue._time import parse_iso as _parse_iso

import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

from taskqueue import __version__
from taskqueue import config as tqconfig
from taskqueue._db_tasks import get_task, get_task_status
from taskqueue.jsonutil import json_list
from taskqueue.capacity import build_capacity
from taskqueue.db import get_config, open_db
from taskqueue.errors import block_kind as _block_kind
from taskqueue.queries import build_task_health, list_attention, list_operator_tasks


TOOLS = [
    {
        "name": "my_tasks",
        "description": "List tasks for an operator (started_by). Requires operator arg or TQ_OPERATOR.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "operator": {"type": "string", "description": "started_by / TQ_OPERATOR"},
                "project": {"type": "string"},
            },
        },
    },
    {
        "name": "capacity",
        "description": "Cluster capacity snapshot (CPU/RAM/GPU free + reservations).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "attention_list",
        "description": (
            "Tasks needing attention (errors / failed / interrupted / suspended). "
            "Optional operator, project, since (ISO timestamp)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "operator": {"type": "string"},
                "project": {"type": "string"},
                "since": {"type": "string", "description": "ISO timestamp lower bound on updated_at"},
            },
        },
    },
    {
        "name": "task_health",
        "description": (
            "Submitted-task health report: status counts, broken/missing depends_on, "
            "attention items, and disabled tasks. Same payload as `tq health --json`."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
            },
        },
    },
    {
        "name": "why_not_running",
        "description": (
            "Why a task is (not) running: the scheduler's last block reason, unmet dependency, "
            "paused project, max_attempts, plus a dry-run verdict for queued tasks. Same as `tq why`."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"project": {"type": "string"}, "task_id": {"type": "string"}},
            "required": ["project", "task_id"],
        },
    },
    {
        "name": "diagnose",
        "description": "Deep bundle for one task (runs, log tail, worker/slave).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "task_id": {"type": "string"},
            },
            "required": ["project", "task_id"],
        },
    },
    {
        "name": "report",
        "description": "Aggregate runs/errors report for recent days.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "days": {"type": "integer"},
            },
        },
    },
    {
        "name": "queue_status",
        "description": (
            "Compact daemon summary (home, port, protocol, counts, "
            "last_boot dirty|clean|unknown, recent daemon_events)."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "drain_notifications",
        "description": (
            "Read task-end events from {home}/notify.jsonl (written for every terminal task). "
            "Optional since (ISO ts), operator (matches started_by or submitted_by), project. "
            "wait_sec (<=100) long-polls until an event arrives. "
            "Does not delete the log — use since as a cursor."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "since": {"type": "string", "description": "ISO timestamp lower bound on event ts"},
                "operator": {"type": "string"},
                "project": {"type": "string"},
                "wait_sec": {"type": "number", "description": "long-poll up to N seconds (cap 100) if nothing new"},
            },
        },
    },
    {
        "name": "log_tail",
        "description": (
            "W6.13/F4: Return the tail of a task's log file. "
            "Optional offset (byte offset from start), max_bytes (default 32768), from_end (bool). "
            "Returns {text, size_bytes, offset, next_offset, log_path, task_status, run_finished}. "
            "run_finished=true means the log stream is complete; next_offset is pinned to size. "
            "Use next_offset as offset on the next call to stream incrementally."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "task_id": {"type": "string"},
                "offset": {"type": "integer", "description": "Byte offset from start of file (default: read from end)"},
                "max_bytes": {"type": "integer", "description": "Max bytes to return (default 32768)"},
                "grep": {"type": "string", "description": "Optional filter: only lines containing this string (case-insensitive)"},
            },
            "required": ["project", "task_id"],
        },
    },
    {
        "name": "enqueue_task",
        "description": (
            "W6.8: Re-queue a task that has reached a terminal state "
            "(failed/interrupted/stopped/completed/suspended/unknown). "
            "Equivalent to `tq retry project/task_id`. "
            "Optional `set`: dict of overridable_args key→value (same as `tq retry --set`). "
            "Returns {ok, status} where ok=false if the task is not in a retriable state."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "task_id": {"type": "string"},
                "set": {
                    "type": "object",
                    "description": (
                        "Optional arg overrides keyed by overridable_args entries, "
                        "e.g. {\"--locales\": \"pl\"}"
                    ),
                },
            },
            "required": ["project", "task_id"],
        },
    },
    {
        "name": "patch_task",
        "description": (
            "W6.8: Update a task's YAML fields (priority, enabled, wall_clock_cap_sec, description). "
            "Pass updates as a dict of key→value. "
            "Returns {ok, applied, message}."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "task_id": {"type": "string"},
                "updates": {
                    "type": "object",
                    "description": "Fields to update, e.g. {\"priority\": 80, \"enabled\": true}",
                },
            },
            "required": ["project", "task_id", "updates"],
        },
    },
    {
        "name": "retry_task",
        "description": (
            "W6.8: Alias for enqueue_task — re-queue a terminal task "
            "(failed/interrupted/stopped/completed/suspended/unknown). "
            "Optional `set` overrides like enqueue_task. Returns {ok, status}."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "task_id": {"type": "string"},
                "set": {
                    "type": "object",
                    "description": "Optional arg overrides (overridable_args keys)",
                },
            },
            "required": ["project", "task_id"],
        },
    },
    {
        "name": "stop_task",
        "description": (
            "W6.8: Request cooperative stop of a running task, or cancel a queued one (it becomes interrupted). "
            "Returns {ok, status, message}."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "task_id": {"type": "string"},
            },
            "required": ["project", "task_id"],
        },
    },
    {
        "name": "submit_task",
        "description": (
            "C1: Submit a YAML task directly without writing a file to disk. "
            "POSTs yaml_text to the master's /api/cli/add route. "
            "Returns {ok, task_ids, errors: [{field, message}]}."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Registered project name"},
                "yaml_text": {"type": "string", "description": "Full YAML content of the task"},
                "filename": {"type": "string", "description": "Optional filename hint (default: unique submitted_*.yaml)"},
                "wait": {"type": "boolean", "description": "Also block until the task settles (see wait_for_task); result gains a 'wait' object"},
                "timeout_sec": {"type": "number", "description": "With wait=true: max seconds (default 60, cap 100)"},
            },
            "required": ["project", "yaml_text"],
        },
    },
    {
        "name": "submit_matrix",
        "description": (
            "C1: Submit a matrix of tasks from a base YAML + list of parameter dicts. "
            "Injects matrix: [...] into base_yaml and submits via the master. "
            "Returns {ok, task_ids, errors}."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "yaml_text": {"type": "string", "description": "Base YAML without matrix key"},
                "matrix": {
                    "type": "array",
                    "description": "List of dicts, each overriding args for one matrix variant",
                    "items": {"type": "object"},
                },
                "filename": {"type": "string"},
            },
            "required": ["project", "yaml_text", "matrix"],
        },
    },
    {
        "name": "list_projects",
        "description": "C2: List all registered projects with their queue_dir and root paths.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "wait_for_task",
        "description": (
            "C3: Block until task(s) settle (completed/failed/interrupted/stopped/suspended; "
            "disabled returns as settled but not finished). Preferred way to learn a task finished. "
            "Select with project+task_id, tasks=[\"proj/id\", ...], or filters project/suite/group/tag/mine "
            "(filters wait only for tasks not yet settled). mode=all (default) or any. "
            "Returns {ok, done, timed_out, waited_sec, tasks:[{status, exit_code, last_error_code, "
            "stop_reason, wall_sec, progress_pct, last_lines, next_actions, log_tail?}], pending, not_found}. "
            "Capped at ~100 s per call (below MCP client timeouts): if timed_out is true, call again. "
            "Then call task_result for metrics/artifacts."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "task_id": {"type": "string"},
                "tasks": {"type": "array", "items": {"type": "string"}, "description": "project/task_id keys"},
                "suite": {"type": "string"},
                "group": {"type": "string"},
                "tag": {"type": "string"},
                "mine": {"type": "boolean", "description": "tasks started by TQ_OPERATOR"},
                "mode": {"type": "string", "enum": ["all", "any"]},
                "include_finished": {"type": "boolean", "description": "filters: also include already-settled tasks"},
                "timeout_sec": {"type": "number", "description": "default 60, capped at 100"},
                "poll_sec": {"type": "number", "description": "max poll interval (default 5)"},
            },
        },
    },
    {
        "name": "agent_usage_summary",
        "description": (
            "Usage analytics: how agents/CLI/UI call taskqueue (calls, errors, avg_ms by channel/client/"
            "operator/project/op/day) and what they submit (tasks, completed/failed, retries, wall-time "
            "p50/p95, top error codes by via/by/client/project/day). Counters only; no payloads."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "window (default 7)"},
                "by": {"type": "string", "description": "usage dims, comma list (default channel,client)"},
                "task_by": {"type": "string", "description": "task dims, comma list (default via,by)"},
                "project": {"type": "string"},
                "channel": {"type": "string"},
                "operator": {"type": "string"},
                "limit": {"type": "integer"},
            },
        },
    },
    {
        "name": "task_result",
        "description": (
            "C4: Structured result for any task: status, exit_code, duration, peak resources, "
            "metrics (from TQ_METRIC lines), artifacts, error_code, block_reason."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "task_id": {"type": "string"},
            },
            "required": ["project", "task_id"],
        },
    },
    {
        "name": "progress_snapshot",
        "description": (
            "Current progress + resource metrics for one or more tasks (same data as the UI metrics dialog / tq metrics). "
            "Returns progress/ETA, requires_gpu, log warn/error/other counts, resources.current/peak/avg "
            "(CPU/RAM/VRAM/GPU util) and history_summary. "
            "project required; task_id optional (all running in project). "
            "Pass include_history=true for sample series (sparklines)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "task_id": {"type": "string", "description": "Specific task; omit for all running in project"},
                "include_history": {"type": "boolean", "description": "Include resource_history samples"},
                "samples": {"type": "integer", "description": "Max history samples (default 60)"},
            },
            "required": ["project"],
        },
    },
]


def _conn(home: Path):
    return open_db(tqconfig.db_path(home), migrate=False)


def _my_tasks(home: Path, args: dict[str, Any]) -> Any:
    operator = (args.get("operator") or os.environ.get("TQ_OPERATOR") or "").strip()
    if not operator:
        raise ValueError("operator required (argument or TQ_OPERATOR env)")
    conn = _conn(home)
    try:
        rows = [dict(r) for r in list_operator_tasks(
            conn, operator=operator, project=args.get("project")
        )]
    finally:
        conn.close()
    return {"tasks": rows, "items": rows, "count": len(rows)}


def _attention(home: Path, args: dict[str, Any]) -> Any:
    conn = _conn(home)
    try:
        items = list_attention(
            conn,
            operator=args.get("operator"),
            project=args.get("project"),
            since=args.get("since"),
        )
    finally:
        conn.close()
    return {"items": items, "count": len(items)}


def _task_health(home: Path, args: dict[str, Any]) -> Any:
    conn = _conn(home)
    try:
        return build_task_health(conn, project=args.get("project"))
    finally:
        conn.close()


def _why_not_running(home: Path, args: dict[str, Any]) -> Any:
    from taskqueue.why import explain_task

    conn = _conn(home)
    try:
        return explain_task(conn, str(args["project"]), str(args["task_id"]))
    finally:
        conn.close()


def _diagnose(home: Path, args: dict[str, Any]) -> Any:
    from taskqueue.diagnose import build_diagnose

    conn = _conn(home)
    try:
        return build_diagnose(conn, args["project"], args["task_id"], home=home)
    finally:
        conn.close()


def _report(home: Path, args: dict[str, Any]) -> Any:
    from taskqueue.reports import build_report

    conn = _conn(home)
    try:
        days = int(args.get("days") or 7)
        return build_report(conn, project=args.get("project"), days=days)
    finally:
        conn.close()


def _queue_status(home: Path, _args: dict[str, Any]) -> Any:
    conn = _conn(home)
    cfg = get_config(conn)
    counts = {
        r["status"]: r["n"]
        for r in conn.execute(
            "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status"
        ).fetchall()
    }
    from taskqueue.db import last_boot_kind, latest_daemon_event, list_daemon_events

    boot = "unknown"
    last_ev = None
    events: list = []
    try:
        boot = last_boot_kind(conn)
        last_ev = latest_daemon_event(conn)
        events = list_daemon_events(conn, limit=5)
    except Exception:
        pass
    conn.close()
    return {
        "home": str(home),
        "port": cfg.get("web_port", 8765),
        "protocol": tqconfig.PROTOCOL_VERSION,
        "schema_version": cfg.get("schema_version"),
        "counts": counts,
        "last_boot": boot,
        "last_daemon_event": last_ev,
        "daemon_events": events,
    }


def _drain_notifications(home: Path, args: dict[str, Any]) -> Any:
    from taskqueue.notify import drain_notifications

    return drain_notifications(
        home,
        since=args.get("since"),
        operator=args.get("operator"),
        project=args.get("project"),
        incremental=True,
        wait_sec=min(float(args.get("wait_sec") or 0.0), _mcp_wait_max_sec()),
    )


# shared definition
from taskqueue.waiting import SETTLED_STATUSES as _TERMINAL_STATUSES  # noqa: E402


def _log_tail(home: Path, args: dict[str, Any]) -> Any:
    """W6.13: Return log tail with optional offset and grep filter.

    F4: response now includes task_status and run_finished so agents know when
    the log stream is done without a second my_tasks call.
    Adds from_end: bool parameter (default true when offset is omitted).
    """
    from taskqueue.logio import MAX_LOG_TAIL_BYTES, clamp_tail_bytes

    project = args.get("project") or ""
    task_id = args.get("task_id") or ""
    if not project or not task_id:
        raise ValueError("project and task_id are required")
    max_bytes = clamp_tail_bytes(int(args.get("max_bytes") or 32768), default=32768)
    grep_pat = args.get("grep")

    conn = _conn(home)
    try:
        run_row = conn.execute(
            "SELECT log_path, status FROM runs WHERE project = ? AND task_id = ? ORDER BY run_id DESC LIMIT 1",
            (project, task_id),
        ).fetchone()
        task_row = get_task_status(conn, project, task_id)
    finally:
        conn.close()

    task_status = task_row["status"] if task_row else None
    run_status = run_row["status"] if run_row else None
    run_finished = run_status in _TERMINAL_STATUSES if run_status else False

    if not run_row or not run_row["log_path"]:
        return {
            "text": "", "size_bytes": 0, "offset": 0, "next_offset": 0, "log_path": None,
            "task_status": task_status, "run_finished": run_finished,
            "message": f"no log found for {project}/{task_id}",
        }

    log_path = Path(run_row["log_path"])
    if not log_path.exists():
        return {
            "text": "", "size_bytes": 0, "offset": 0, "next_offset": 0, "log_path": str(log_path),
            "task_status": task_status, "run_finished": run_finished,
            "message": "log file missing",
        }

    size = log_path.stat().st_size
    offset_arg = args.get("offset")
    from_end = bool(args.get("from_end", offset_arg is None))
    if offset_arg is None:
        offset = max(0, size - max_bytes) if from_end else 0
    else:
        offset = max(0, int(offset_arg))

    with open(log_path, "rb") as fh:
        fh.seek(offset)
        chunk = fh.read(min(max_bytes, MAX_LOG_TAIL_BYTES))

    text = chunk.decode("utf-8", errors="replace")
    next_offset = offset + len(chunk)
    # Done only once the whole file has been returned (a partial page must not skip data).
    stream_done = bool(run_finished and next_offset >= size)

    if grep_pat:
        pat_lower = grep_pat.lower()
        lines = text.splitlines(keepends=True)
        text = "".join(ln for ln in lines if pat_lower in ln.lower())

    return {
        "text": text,
        "size_bytes": size,
        "offset": offset,
        "next_offset": next_offset,
        "log_path": str(log_path),
        "task_status": task_status,
        "run_finished": run_finished,
        "stream_done": stream_done,
    }


def _enqueue_task(home: Path, args: dict[str, Any]) -> Any:
    """W6.8+S7: Re-queue a terminal task, optionally with per-run arg overrides."""
    import json as _json
    from taskqueue.db import get_task
    from taskqueue.mutations import retry_task_op

    project = args.get("project") or ""
    task_id = args.get("task_id") or ""
    set_overrides: dict[str, Any] = args.get("set") or {}
    if not project or not task_id:
        raise ValueError("project and task_id are required")
    conn = _conn(home)
    try:
        row = get_task(conn, project, task_id)
        if row is None:
            return {"ok": False, "status": None, "message": f"task not found: {project}/{task_id}"}
        current_status = row["status"]
        from taskqueue._db_tasks import run_summary
        runs_before = run_summary(conn, project, task_id)["run_number"]

        # args_override must be a JSON list of "key=val" strings, not a dict.
        # Use f"{k}={v}" unconditionally so falsy values (0, False, "") are preserved.
        _override_list: list[str] = [f"{k}={v}" for k, v in set_overrides.items()]
        # S7: same allowlist check as the web retry route.
        if _override_list:
            from taskqueue.db import check_arg_overrides

            _err = check_arg_overrides(row, _override_list)
            if _err:
                return {"ok": False, "status": current_status, "message": _err}
    finally:
        conn.close()

    result = retry_task_op(
        home, project, task_id,
        args_override=_override_list or None,
    )
    ok = bool(result.get("ok"))
    return {
        "ok": ok,
        "status": "queued" if ok else current_status,
        "message": "re-queued" if ok else (result.get("error") or f"not retriable (status={current_status})"),
        # The re-run has started once run_number (task_result / wait_for_task) exceeds this.
        "run_number_before": runs_before,
        **({"next": _wait_next([f"{project}/{task_id}"])} if ok else {}),
    }


def _patch_task(home: Path, args: dict[str, Any]) -> Any:
    """W6.8: Patch a task's YAML fields."""
    from pathlib import Path as _Path

    from taskqueue.config import load_projects
    from taskqueue.db import get_config, get_task, parse_task_yaml, upsert_task
    from taskqueue.yaml_patch import patch_task_yaml

    project = args.get("project") or ""
    task_id = args.get("task_id") or ""
    updates = args.get("updates") or {}
    if not project or not task_id:
        raise ValueError("project and task_id are required")
    if not isinstance(updates, dict):
        raise ValueError("updates must be a dict")

    conn = _conn(home)
    try:
        row = get_task(conn, project, task_id)
        if row is None or not row["yaml_source"]:
            return {"ok": False, "applied": [], "message": f"task not found or has no yaml_source: {project}/{task_id}"}
        from taskqueue.master_client import via_master
        api = via_master(
            home, "POST", f"/api/tasks/{project}/{task_id}/patch", body={"updates": updates}
        )
        if api is not None:
            return {
                "ok": bool(api.get("ok")),
                "applied": api.get("applied") or [],
                "message": api.get("error") or f"patched {', '.join(api.get('applied') or [])}",
            }
        path = _Path(row["yaml_source"])
        try:
            applied = patch_task_yaml(path, updates, task_id=task_id)
        except ValueError as exc:
            return {"ok": False, "applied": [], "message": str(exc)}
        projects = load_projects(home)
        proj = projects.get(project)
        if proj is None:
            return {"ok": False, "applied": applied, "message": f"project not registered: {project}"}
        upsert_task(conn, parse_task_yaml(
            path, proj, allow_remote_notify=bool(get_config(conn).get("notify_allow_remote"))
        ))
    finally:
        conn.close()
    return {"ok": True, "applied": applied, "message": f"patched {', '.join(applied)}"}


def _stop_task(home: Path, args: dict[str, Any]) -> Any:
    """W6.8: Stop a running task, or cancel a queued one (marked interrupted)."""
    from taskqueue.db import get_task, request_stop_with_control

    project = args.get("project") or ""
    task_id = args.get("task_id") or ""
    if not project or not task_id:
        raise ValueError("project and task_id are required")
    conn = _conn(home)
    try:
        row = get_task(conn, project, task_id)
        if row is None:
            return {"ok": False, "status": None, "message": f"task not found: {project}/{task_id}"}
        current_status = row["status"]
        if current_status not in ("running", "queued"):
            return {"ok": False, "status": current_status, "message": f"task is not running or queued (status={current_status})"}
        from taskqueue.master_client import via_master
        # Same as the web UI: a queued task is marked interrupted, a running one gets a stop request.
        api = via_master(home, "POST", f"/api/stop/{project}/{task_id}")
        if api is not None:
            ok = bool(api.get("ok"))
            final = api.get("final_status")
            return {
                "ok": ok,
                "status": final or current_status,
                "message": ("stopped before start" if final else "stop requested") if ok else (api.get("error") or "stop failed"),
            }
        if current_status == "queued":
            return {"ok": False, "status": "queued", "message": "stopping a queued task needs the master (daemon not reachable)"}
        request_stop_with_control(conn, home, project, task_id)
    finally:
        conn.close()
    return {"ok": True, "status": "running", "message": "stop requested"}


def _progress_snapshot(home: Path, args: dict[str, Any]) -> Any:
    """W6.14: Return progress + resource metrics for tasks in a project."""
    from taskqueue.metrics_snapshot import build_run_metrics

    project = args.get("project")
    if not project:
        raise ValueError("project is required")
    task_id = args.get("task_id")
    include_history = bool(args.get("include_history"))
    samples = int(args.get("samples") or 60)

    conn = _conn(home)
    try:
        if task_id:
            rows = conn.execute(
                "SELECT task_id FROM tasks WHERE project = ? AND task_id = ?",
                (project, task_id),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT task_id FROM tasks WHERE project = ? AND status = 'running'",
                (project,),
            ).fetchall()
        result = []
        for row in rows:
            m = build_run_metrics(
                conn, project, row["task_id"],
                home=home, samples=samples, include_history=include_history,
            )
            # Keep legacy top-level keys for existing agents.
            result.append({
                "task_id": m["task_id"],
                "status": m["status"],
                "progress_pct": m["progress_pct"],
                "eta_sec": m["eta_sec"],
                "last_line": m["last_line"],
                "elapsed_sec": m["elapsed_sec"],
                "requires_gpu": m["requires_gpu"],
                "log": m["log"],
                "metrics": m["metrics"],
                "run_id": m["run_id"],
                "resources": m["resources"],
                "history_summary": m["history_summary"],
                "source": m["source"],
                **({"history": m["history"]} if include_history else {}),
            })
    finally:
        conn.close()
    return {"project": project, "tasks": result, "items": result, "count": len(result)}


def _submit_task(home: Path, args: dict[str, Any]) -> Any:
    """C1: Submit a YAML task (or matrix); with ``wait=true`` also block until it settles."""
    result = _submit_task_nowait(home, args)
    if args.get("wait") and result.get("ok") and result.get("task_ids"):
        project = args.get("project") or ""
        result["wait"] = _wait_for_task(home, {
            "tasks": [f"{project}/{t}" for t in result["task_ids"]],
            "mode": "all",
            "timeout_sec": args.get("timeout_sec"),
        })
    elif result.get("ok") and result.get("task_ids"):
        project = args.get("project") or ""
        result["next"] = _wait_next([f"{project}/{t}" for t in result["task_ids"]])
    return result


def _wait_next(keys: list[str]) -> dict[str, Any]:
    """Suggested follow-up after a task is created or re-queued (a hint, not an action)."""
    from taskqueue.waiting import wait_command

    return {
        "tool": "wait_for_task",
        "arguments": {"tasks": keys, "mode": "all", "timeout_sec": 60},
        "cli": wait_command(keys),
        "note": (
            "Repeat wait_for_task while timed_out, then task_result. "
            "Claude Code: run the cli line with run_in_background to be woken when it settles."
        ),
    }


def _submit_task_nowait(home: Path, args: dict[str, Any]) -> Any:
    from taskqueue.master_client import via_master
    from taskqueue.config import load_projects
    from taskqueue.db import get_config, open_db, parse_task_yaml_many, upsert_task

    project = args.get("project") or ""
    yaml_text = args.get("yaml_text") or ""
    filename = args.get("filename") or ""
    matrix = args.get("matrix")  # optional list of dicts for submit_matrix

    if not project or not yaml_text.strip():
        return {"ok": False, "task_ids": [], "errors": [{"field": "project/yaml_text", "message": "project and yaml_text are required"}]}

    # inject matrix into yaml_text if provided via submit_matrix
    if matrix is not None:
        try:
            import yaml as _yaml
            doc = _yaml.safe_load(yaml_text) or {}
            if not isinstance(doc, dict):
                return {"ok": False, "task_ids": [], "errors": [{"field": "yaml_text", "message": "YAML must be a mapping"}]}
            doc["matrix"] = matrix
            yaml_text = _yaml.dump(doc, allow_unicode=True)
        except Exception as exc:
            return {"ok": False, "task_ids": [], "errors": [{"field": "matrix", "message": str(exc)}]}

    api = via_master(home, "POST", "/api/cli/add", body={"project": project, "yaml_text": yaml_text, "filename": filename})
    if api is not None:
        if api.get("ok"):
            return {"ok": True, "task_ids": api.get("task_ids") or [], "errors": []}
        err = api.get("error") or "submission failed"
        return {"ok": False, "task_ids": [], "errors": [{"field": "", "message": err}]}

    # offline: write directly to DB
    projects = load_projects(home)
    proj = projects.get(project)
    if proj is None:
        return {"ok": False, "task_ids": [], "errors": [{"field": "project", "message": f"Unknown project '{project}'"}]}
    import time as _time
    import uuid as _uuid
    from pathlib import Path as _P

    # Mirror /api/cli/add: persist into the project's queue dir (so yaml_source
    # stays valid for resync), validated from a scratch file first.
    fname = _P(filename).name if filename else ""
    if fname and not fname.endswith((".yaml", ".yml")):
        fname += ".yaml"
    if not fname:
        fname = f"submitted_{int(_time.time() * 1000)}_{_uuid.uuid4().hex[:6]}.yaml"
    dest = proj.queue_dir / fname
    dest.parent.mkdir(parents=True, exist_ok=True)
    scratch = dest.with_name(f".{fname}.{_uuid.uuid4().hex[:8]}.tmp")
    db = open_db(tqconfig.db_path(home), migrate=False)
    try:
        allow_remote = bool(get_config(db).get("notify_allow_remote"))
        try:
            scratch.write_text(yaml_text, encoding="utf-8")
            parse_task_yaml_many(scratch, proj, allow_remote_notify=allow_remote)
            os.replace(scratch, dest)
            tasks = parse_task_yaml_many(dest, proj, allow_remote_notify=allow_remote)
        except Exception as exc:
            scratch.unlink(missing_ok=True)
            return {"ok": False, "task_ids": [], "errors": [{"field": "yaml_text", "message": str(exc)}]}
        ids = []
        from taskqueue.db import stamp_provenance
        for task in tasks:
            upsert_task(db, task)
            ids.append(task.task_id)
        stamp_provenance(db, project, ids, "mcp", os.environ.get("TQ_OPERATOR"), _CLIENT_INFO["name"])
        db.commit()
    finally:
        db.close()
    return {"ok": True, "task_ids": ids, "errors": []}


def _list_projects(home: Path, _args: dict[str, Any]) -> Any:
    """C2: List registered projects."""
    from taskqueue.config import load_projects
    projects = load_projects(home)
    result = []
    for name, proj in projects.items():
        result.append({
            "name": name,
            "queue_dir": str(proj.queue_dir),
            "root": str(proj.root),
        })
    return {"projects": result, "items": result, "count": len(result)}


MCP_WAIT_DEFAULT_SEC = 60.0


def _mcp_wait_max_sec() -> float:
    """Hard cap per wait call; stay at/below the documented 100s (env cannot raise it)."""
    try:
        return min(100.0, max(5.0, float(os.environ.get("TQ_MCP_WAIT_MAX_SEC") or 100.0)))
    except ValueError:
        return 100.0


def _wait_for_task(home: Path, args: dict[str, Any], on_tick: Any = None) -> Any:
    """Block until selected task(s) settle (completed/failed/...) or the timeout hits.

    Selectors: ``project``+``task_id``, ``tasks`` (list of ``project/task_id``),
    or filters ``project`` / ``suite`` / ``group`` / ``tag`` / ``mine`` (tasks that
    have not settled yet). ``mode`` is ``all`` (default) or ``any``.
    """
    from taskqueue.waiting import resolve_selectors, wait_for

    project = (args.get("project") or "").strip()
    task_id = (args.get("task_id") or "").strip()
    keys = [str(k) for k in (args.get("tasks") or [])]
    if project and task_id:
        keys.insert(0, f"{project}/{task_id}")
        project = ""
    tag = args.get("tag")
    tags = [tag] if isinstance(tag, str) and tag else list(args.get("tags") or [])
    started_by = None
    if args.get("mine"):
        started_by = (os.environ.get("TQ_OPERATOR") or "").strip()
        if not started_by:
            raise ValueError("mine=true requires TQ_OPERATOR in the MCP environment")
    suite = args.get("suite") or None
    group = args.get("group") or None
    if not (keys or project or tags or suite or group or started_by):
        raise ValueError("pass project+task_id, tasks, or a filter (project/suite/group/tag/mine)")
    mode = args.get("mode") or "all"
    cap = _mcp_wait_max_sec()
    timeout_sec = min(float(args.get("timeout_sec") or MCP_WAIT_DEFAULT_SEC), cap)
    poll_sec = float(args.get("poll_sec") or 5)

    conn = _conn(home)
    try:
        resolved = resolve_selectors(
            conn, keys=keys, project=project or None, tags=tags, suite=suite,
            group=group, started_by=started_by,
            include_finished=bool(args.get("include_finished")),
        )
    finally:
        conn.close()
    result = wait_for(
        lambda: _conn(home), resolved, mode=mode, timeout_sec=timeout_sec,
        poll_sec=poll_sec, on_tick=on_tick,
    )
    _attach_failure_details(home, result)
    if len(result["tasks"]) == 1:  # back-compat flat shape for single-task callers
        t = result["tasks"][0]
        result.update(status=t["status"], exit_code=t["exit_code"], last_lines=t["last_lines"])
    return result


def _attach_failure_details(home: Path, result: dict[str, Any], limit: int = 5) -> None:
    """Inline a short log tail for settled non-completed tasks (saves a round trip)."""
    n = 0
    for t in result["tasks"]:
        if t["settled"] and t["status"] not in ("completed",) and n < limit:
            n += 1
            try:
                tail = _log_tail(home, {"project": t["project"], "task_id": t["task_id"], "max_bytes": 2048})
                t["log_tail"] = tail.get("text", "") if isinstance(tail, dict) else ""
            except Exception:
                t["log_tail"] = ""


def _task_result(home: Path, args: dict[str, Any]) -> Any:
    """C4: Structured result for a completed task."""
    import json as _json
    from taskqueue.db import get_task

    project = args.get("project") or ""
    task_id = args.get("task_id") or ""
    if not project or not task_id:
        raise ValueError("project and task_id are required")

    conn = _conn(home)
    try:
        task = get_task(conn, project, task_id)
        if task is None:
            return {"ok": False, "message": f"task not found: {project}/{task_id}"}
        # fetch last run
        run = conn.execute(
            "SELECT * FROM runs WHERE project = ? AND task_id = ? ORDER BY run_id DESC LIMIT 1",
            (project, task_id),
        ).fetchone()
        from taskqueue._db_tasks import run_summary
        run_info = run_summary(conn, project, task_id)
    finally:
        conn.close()

    duration_sec = None
    run_id = None
    exit_code = None
    peak_ram_gib = None
    peak_vram_gib = None
    metrics: dict = {}
    if run:
        run_id = run["run_id"]
        exit_code = run["exit_code"]
        peak_ram_gib = run["peak_ram_gib"]
        peak_vram_gib = run["peak_vram_gib"]
        if run["started_at"] and run["finished_at"]:
            try:
                from datetime import datetime
                s = _parse_iso(run["started_at"])
                f = _parse_iso(run["finished_at"])
                duration_sec = round((f - s).total_seconds(), 1)
            except Exception:
                pass
        if run["metrics_json"]:
            try:
                metrics = _json.loads(run["metrics_json"])
            except Exception:
                pass

    artifacts = []
    try:
        raw_arts = json_list(task["artifacts"])
        from pathlib import Path as _P
        for a in raw_arts:
            artifacts.append({"path": str(a), "exists": _P(str(a)).exists()})
    except Exception:
        pass

    return {
        "ok": True,
        "status": task["status"],
        "exit_code": exit_code,
        "run_id": run_id,
        "run_number": run_info["run_number"],
        "last_run_started_at": run_info["last_run_started_at"],
        "last_run_finished_at": run_info["last_run_finished_at"],
        "duration_sec": duration_sec,
        "progress_pct": run["progress_pct"] if run else None,
        "peak_ram_gib": peak_ram_gib,
        "peak_vram_gib": peak_vram_gib,
        "last_lines": [run["last_log_line"]] if run and run["last_log_line"] else [],
        "metrics": metrics,
        "artifacts": artifacts,
        "error_code": task["last_error_code"],
        "block_reason": task["block_reason"],
        "block_kind": _block_kind(task["last_error_code"]),
    }


_PIGGY: dict[str, Any] = {"ts": None, "size": -1}


def _finished_since_last_call(home: Path) -> list[dict[str, Any]]:
    """Terminal events for this operator since the previous tool call (per MCP process).

    Lets an agent notice completions on any call, with no extra polling. Cheap: a stat()
    when nothing was appended. Only active when TQ_OPERATOR is set.
    """
    from taskqueue._time import _now as _now_iso
    from taskqueue.notify import TERMINAL_STATUSES, iter_notify_events, notify_log_path

    op = (os.environ.get("TQ_OPERATOR") or "").strip()
    if not op:
        return []
    if _PIGGY["ts"] is None:
        _PIGGY["ts"] = _now_iso()  # never replay history
        return []
    path = notify_log_path(home)
    try:
        size = path.stat().st_size
    except OSError:
        return []
    if size == _PIGGY["size"]:
        return []
    _PIGGY["size"] = size
    evs = iter_notify_events(path, since=_PIGGY["ts"], operator=op, limit=20)
    if not evs:
        return []
    _PIGGY["ts"] = str(evs[-1].get("ts") or _PIGGY["ts"])
    keep = ("ts", "project", "task_id", "status", "exit_code", "last_error_code", "stop_reason")
    return [{k: e.get(k) for k in keep if e.get(k) is not None} for e in evs if e.get("status") in TERMINAL_STATUSES]


def _agent_usage_summary(home: Path, args: dict[str, Any]) -> Any:
    from taskqueue.agent_stats import agent_usage

    conn = _conn(home)
    try:
        return agent_usage(
            conn, days=int(args.get("days") or 7), by=args.get("by"), task_by=args.get("task_by"),
            project=args.get("project") or None, channel=args.get("channel") or None,
            operator=args.get("operator") or None, limit=int(args.get("limit") or 200),
        )
    finally:
        conn.close()


def _capacity(home: Path, args: dict[str, Any]) -> Any:
    conn = _conn(home)
    try:
        return build_capacity(conn)
    finally:
        conn.close()


# One dispatch table; tests/test_mcp_errors.py checks it names exactly the TOOLS schemas.
_HANDLERS: dict[str, Any] = {
    "my_tasks": _my_tasks,
    "capacity": _capacity,
    "attention_list": _attention,
    "task_health": _task_health,
    "diagnose": _diagnose,
    "why_not_running": _why_not_running,
    "report": _report,
    "queue_status": _queue_status,
    "drain_notifications": _drain_notifications,
    "progress_snapshot": _progress_snapshot,
    "log_tail": _log_tail,
    "enqueue_task": _enqueue_task,
    "retry_task": _enqueue_task,
    "patch_task": _patch_task,
    "stop_task": _stop_task,
    "submit_task": _submit_task,
    "submit_matrix": _submit_task,
    "list_projects": _list_projects,
    "wait_for_task": _wait_for_task,
    "task_result": _task_result,
    "agent_usage_summary": _agent_usage_summary,
}


def _call_tool(home: Path, name: str, args: dict[str, Any]) -> Any:
    fn = _HANDLERS.get(name)
    if fn is None:
        raise ValueError(f"unknown tool: {name}")
    return fn(home, args)


_CLIENT_INFO: dict[str, str] = {"name": "mcp"}  # replaced from initialize clientInfo
_OUT_LOCK = threading.Lock()  # waits run on worker threads; keep stdout lines whole


def _emit(payload: dict[str, Any]) -> None:
    with _OUT_LOCK:
        print(json.dumps(payload), flush=True)


def _respond(msg_id: Any, result: Any) -> None:
    _emit({"jsonrpc": "2.0", "id": msg_id, "result": result})


def _respond_error(msg_id: Any, code: int, message: str) -> None:
    _emit({"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}})


def _record_usage(home: Path, name: Any, args: dict[str, Any], t0: float, error_code: str) -> None:
    """Count one MCP tool call (name + timing only; never arguments). Fail-open."""
    import time as _time

    try:
        from taskqueue.usage_track import record_direct

        conn = _conn(home)
        try:
            record_direct(
                conn, "mcp", str(name or "?"),
                client=_CLIENT_INFO["name"],
                operator=(args.get("operator") or os.environ.get("TQ_OPERATOR") or ""),
                project=str(args.get("project") or ""),
                ms=(_time.monotonic() - t0) * 1000.0,
                error_code=error_code,
            )
        finally:
            conn.close()
    except Exception:
        pass


def _run_tool_call(home: Path, msg_id: Any, params: dict[str, Any]) -> None:
    import time as _time

    name = params.get("name")
    args = params.get("arguments") or {}
    t0 = _time.monotonic()
    err = ""
    spec = next((t for t in TOOLS if t.get("name") == name), None)
    if spec is None or not isinstance(args, dict):
        _record_usage(home, name, args if isinstance(args, dict) else {}, t0, "mcp.error")
        _respond_error(msg_id, -32602, f"Unknown tool: {name}" if spec is None else "arguments must be an object")
        return
    missing = [k for k in (spec.get("inputSchema") or {}).get("required") or [] if k not in args]
    if missing:
        _record_usage(home, name, args, t0, "mcp.bad_args")
        _respond_tool_error(msg_id, f"missing required argument(s): {', '.join(missing)}")
        return
    try:
        if name == "wait_for_task":
            token = (params.get("_meta") or {}).get("progressToken")
            last = [0.0]

            def _tick(info: dict[str, Any]) -> None:
                if token is None or info["waited_sec"] - last[0] < 10:
                    return
                last[0] = info["waited_sec"]
                _emit({
                    "jsonrpc": "2.0",
                    "method": "notifications/progress",
                    "params": {
                        "progressToken": token,
                        "progress": info["waited_sec"],
                        "message": f"waiting on {len(info['pending'])} task(s)",
                    },
                })

            result = _wait_for_task(home, args, on_tick=_tick)
        else:
            result = _call_tool(home, name, args)
        if (
            isinstance(result, dict)
            and name not in ("wait_for_task", "drain_notifications")
            and not args.get("wait")
        ):
            try:
                fin = _finished_since_last_call(home)
                if fin:
                    result["finished_since_last_call"] = fin
            except Exception:
                pass
        if isinstance(result, dict) and result.get("ok") is False:
            err = str(result.get("error_code") or "ok_false")
        elif isinstance(result, dict) and result.get("timed_out"):
            err = "wait.timeout"
        _record_usage(home, name, args, t0, err)
        _respond(msg_id, {
            "content": [{"type": "text", "text": json.dumps(result, indent=2, default=str)}],
            "structuredContent": result,
        })
    except Exception as exc:
        _record_usage(home, name, args, t0, "mcp.error")
        # MCP: a failing tool is a tool *result* with isError, so the model sees the message.
        _respond_tool_error(msg_id, str(exc))


def _respond_tool_error(msg_id: Any, message: str) -> None:
    _respond(msg_id, {"content": [{"type": "text", "text": message}], "isError": True})


def run_stdio(home: Path | None = None) -> int:
    home = home or tqconfig.state_home()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            _respond_error(None, -32700, "Parse error")
            continue
        if not isinstance(msg, dict):
            _respond_error(None, -32600, "Invalid Request")
            continue
        msg_id = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}
        if not isinstance(params, dict):
            params = {}
        if method == "initialize":
            ci = params.get("clientInfo") or {}
            if ci.get("name"):
                _CLIENT_INFO["name"] = f"mcp/{ci.get('name')} {ci.get('version', '')}".strip()[:64]
            from taskqueue import master_client as _mc
            _mc.set_identity("mcp", _CLIENT_INFO["name"])
            _respond(msg_id, {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "taskqueue", "version": __version__},
            })
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            _respond(msg_id, {"tools": TOOLS})
        elif method == "tools/call":
            _a = params.get("arguments") or {}
            if params.get("name") == "wait_for_task" or _a.get("wait") or (
                params.get("name") == "drain_notifications" and _a.get("wait_sec")
            ):
                # Blocking: run off the read loop so other tools stay responsive.
                threading.Thread(
                    target=_run_tool_call, args=(home, msg_id, params), daemon=True
                ).start()
            else:
                _run_tool_call(home, msg_id, params)
        elif method == "ping":
            _respond(msg_id, {})
        else:
            if msg_id is not None:
                _respond_error(msg_id, -32601, f"Method not found: {method}")
    return 0
