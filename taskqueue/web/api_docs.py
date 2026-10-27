"""API documentation registry and HTML renderer."""
from __future__ import annotations

from typing import Any, Callable

from taskqueue.db import SCHEMA_VERSION

_API_REGISTRY: list[dict[str, Any]] = []
_api_docs_html_cache: str | None = None


def api_doc(
    method: str,
    path: str,
    summary: str,
    *,
    auth: bool = True,
    **kw: Any,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register an endpoint in ``_API_REGISTRY``. Use on handlers or stubs."""

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        entry: dict[str, Any] = {
            "method": method,
            "path": path,
            "summary": summary,
            "auth": auth,
        }
        entry.update(kw)
        _API_REGISTRY.append(entry)
        return fn

    return decorator


def _register_api_docs() -> None:
    """Populate _API_REGISTRY with all known endpoints via ``api_doc``."""

    def _e(method: str, path: str, summary: str, **kw: Any) -> None:
        @api_doc(method, path, summary, **kw)
        def _stub() -> None:
            pass

    _e("GET", "/api/status", "Running tasks, queued count, system info", returns="StatusObject")
    _e("GET", "/api/dashboard", "Unified web UI snapshot (tab=queue|history|workers|dashboard)", params={"tab": "Active tab", "project": "Filter by project", "status": "Queue status filter", "started_by": "Operator scope (mine view)", "offset": "Page offset", "limit": "Page size"})
    _e("GET", "/api/queue", "All tasks (paginated, server-side filtered)", params={"project": "Filter by project", "status": "Filter by status", "started_by": "Filter by started_by operator", "search": "Substring match on task_id/description", "offset": "Page offset", "limit": "Page size (default 200, max 5000)"})
    _e("GET", "/api/history", "Recent runs", params={"project": "Filter by project name", "started_by": "Filter by task started_by", "search": "Substring match on task_id/project/run_id/error_category/worker_id", "offset": "Page offset", "limit": "Page size"})
    _e("GET", "/api/config", "Daemon configuration (tokens redacted)")
    _e("GET", "/api/projects", "Registered project names")
    _e("GET", "/api/my-projects", "Projects where an operator started or submitted a task", params={"operator": "Operator name (started_by / submitted_by)"})
    _e("GET", "/api/project-limits", "Per-project concurrency/RAM limits")
    _e("GET", "/api/locks", "Named resource lock occupancy + contention vs project max_tasks")
    _e("GET", "/api/workers", "Registered remote workers")
    _e("GET", "/api/attention", "Tasks needing attention (blocked, failed, etc.)")
    _e("GET", "/api/capacity", "Current scheduler capacity and headroom")
    _e("POST", "/api/audit/<id>/rollback", "Restore the config values recorded before a config audit event (secrets and unknown keys are skipped)")
    _e("POST", "/api/sse-ticket", "Mint a 120 s ticket for EventSource URLs (?tq_ticket=) so the API token stays out of URLs; valid only for /api/events and /api/logs/<id>/stream")
    _e("GET", "/api/plan", "Read-only admission dry-run: which queued tasks would start next, with block reasons (limit=1..200; declared resources only)")
    _e("GET", "/api/report", "Usage and cost report")
    _e("GET", "/api/diagnose", "Diagnostic summary")
    _e("GET", "/api/cost", "Task cost history")
    _e("GET", "/api/deps", "Dependency graph nodes + edges (unfocused graphs are capped at 500 nodes; see truncated/total_nodes)", params={
        "project": "Filter to one project",
        "focus": "task_id or project/task_id — ancestors + descendants",
        "mode": "all (default) | active | attention — incomplete chains + upstream stubs",
        "suite": "Filter to tasks in this suite name",
        "format": "json (default) | dot — Graphviz DOT text",
    })
    _e("GET", "/api/metrics", "Prometheus text metrics: tasks by status, running per project, workers by state, daemon RSS")
    _e("GET", "/api/hardware", "CPU/GPU hardware snapshot")
    _e("GET", "/api/fits", "Which GPUs fit a task now", params={"vram_gib": "GPU VRAM needed", "ram_gib": "Host RAM needed"})
    _e("GET", "/api/usage", "Historical resource-usage time series")
    _e("GET", "/api/runs/<run_id>", "One run: status, peaks, metrics, the args/env-name snapshot taken at start, and an `anomaly` block (peaks/wall above 1.5x the p95 of the last 20 completed runs)")
    _e("GET", "/api/log-search", "Search recent run log tails across tasks", params={"q": "Text (case-insensitive)", "project": "Limit to project", "runs": "Recent runs to scan (max 200)", "limit": "Max matches (max 500)"})
    _e("GET", "/api/runs/diff", "Differences between two runs (?a=&b= run ids): status, peaks, args, env names, metrics")
    _e("GET", "/api/notify/dead", "Webhooks that exhausted their retries (dead-letter list; ?limit=)")
    _e("GET", "/api/notifications", "Recent webhook notification events")
    _e("GET", "/api/tasks", "Tasks with filtering", params={"project": "Project", "started_by": "Operator", "q": "Text search on task_id/description", "limit": "Page size (default 2000, max 10000)", "offset": "Rows to skip"})
    _e("GET", "/api/logs/<run_id>", "Last N bytes of run log", params={"bytes": "Max bytes (default 256KiB)"})
    _e("GET", "/api/tasks/<project>/<task_id>/yaml", "Raw YAML source for a task")
    _e("GET", "/api/tasks/<project>/<task_id>/runs", "All runs for a specific task")
    _e("GET", "/api/tasks/<project>/<task_id>/log-search", "Search run log", params={"q": "Regex/text pattern", "offset": "Byte offset", "max_bytes": "Window size"})
    _e("GET", "/api/events", "SSE stream of queue events")
    _e("GET", "/api/docs", "This page — HTML API reference", auth=False)
    _e("GET", "/api/docs.json", "Machine-readable API registry (OpenAPI-shaped)", auth=False)
    _e("POST", "/api/stop/<project>/<task_id>", "Stop a running or queued task", params={"reason": "Optional stop reason stored on the latest run", "operator": "Caller identity", "force": "1 to override ownership"})
    _e("POST", "/api/tasks/<project>/<task_id>/retry", "Re-queue a single retryable task")
    _e("POST", "/api/retry/<project>/<task_id>", "Re-queue a single retryable task (legacy alias)")
    _e("POST", "/api/runs/<run_id>/rerun", "Re-queue the run's task with that run's recorded args (must satisfy overridable_args)")
    _e("POST", "/api/tasks/bulk", "Bulk stop/priority/pause/resume", body="{action: stop|priority|pause|resume, tasks: [\"p/t\",…], priority?}")
    _e("POST", "/api/tasks/retry-batch", "Batch re-queue tasks", body="{project?, status?: [...], task_ids?: [\"p/t\",…]}")
    _e("POST", "/api/enable/<project>/<task_id>", "Enable or disable a task", params={"enabled": "1 or 0"})
    _e("POST", "/api/priority/<project>/<task_id>/<N>", "Set task priority (0–100)")
    _e("POST", "/api/config", "Update daemon config key/value pairs", body="JSON {key: value}")
    _e("POST", "/api/project-limits/<project>", "Set per-project limits", body="JSON {max_concurrent, max_ram_gib, …}")
    _e("POST", "/api/project/freeze/<project>", "Freeze a project (no new tasks dispatched)")
    _e("POST", "/api/project/unfreeze/<project>", "Unfreeze a project")
    _e("POST", "/api/force/<project>/<task_id>", "Force-run a task immediately")
    _e("GET", "/api/worker/next-task", "Worker: claim next eligible task", params={"worker_id": "Worker ID", "protocol": "Protocol version"})
    _e("GET",  "/api/worker/version", "W7.5: Master version info + upgrade_url/sha256 for auto-upgrade")
    _e("GET",  "/api/worker/wheel", "W7.5: Download pre-built worker wheel (requires auth)")
    _e("POST", "/api/worker/register", "Worker: register or update worker metadata", body="WorkerInfo JSON")
    _e("POST", "/api/worker/heartbeat", "Worker: renew lease and check for stop signal", body="{worker_id, run_id?, protocol, version, schema}")
    _e("POST", "/api/worker/finalize", "Worker: mark task completed/failed", body="{run_id, worker_id, status, exit_code, …}")
    _e("POST", "/api/worker/log-chunk", "Worker: append base64 log chunk", body="{run_id, worker_id, chunk_b64, seq?}")
    _e("GET", "/api/tags", "Distinct task tags", params={"project": "Filter by project"})
    _e("GET", "/api/groups", "Task groups with member counts")
    _e("GET", "/api/suites", "Suite names with member tasks")
    _e("GET", "/api/project-roots", "Map of project name → filesystem root")
    _e("GET", "/api/help", "Context-sensitive help for web UI", params={"tab": "Active UI tab", "lang": "Locale code (en/pl/zh/ar)"}, auth=False)
    _e("GET", "/api/agent-usage", "Agent/API interaction counters + task submissions and outcomes by provenance", params={"days": "Window in days (default 7)", "by": "Usage dims (comma list): channel,client,operator,project,op,error_code,day,hour", "task_by": "Task dims: via,by,client,project,day", "project": "Filter by project", "channel": "Filter by channel", "operator": "Filter by operator", "limit": "Max rows per view (default 200)"})
    _e("GET", "/api/task-stats", "Per-task duration percentiles (avg/p50/p95 from completed)", params={"project": "Filter by project", "task_id": "Optional task filter"})
    _e("GET", "/api/timeline", "Forward forecast Gantt (queued/running + cron)", params={"project": "Filter by project", "hours": "Forward window hours"})
    _e("GET", "/api/schedules", "Tasks that have schedule.cron, with the next fire", params={"project": "Filter by project"})
    _e("GET", "/api/alerts", "Legacy custom alert rules (deprecated; no longer evaluated)")
    _e("GET", "/api/alerts/system", "Built-in system alerts (enabled / windows toast / firing)")
    _e("GET", "/api/health", "Queue health light: level red/amber/green, parts, counts, sample task keys")
    _e("GET", "/api/audit", "Operator audit log", params={
        "limit": "Max events (1-2000)",
        "actor": "Exact actor", "action": "Exact action", "project": "Exact project",
        "since": "ISO timestamp, inclusive lower bound", "until": "ISO timestamp, inclusive upper bound",
        "before_id": "Return events with id below this (pagination)",
        "format": "json (default) | csv | jsonl",
    })
    _e("GET", "/api/tokens", "Scoped API tokens (secrets are not returned)")
    _e("POST", "/api/alerts", "Create a legacy alert rule", body="JSON {name, kind, threshold, ...}")
    _e("POST", "/api/health/ack", "Acknowledge current failures (health red -> amber until the next one)")
    _e("PATCH", "/api/alerts/<id>", "Update a legacy alert rule")
    _e("PATCH", "/api/alerts/system/<key>", "Update system alert toggles", body="JSON {enabled?, windows_toast?}")
    _e("DELETE", "/api/alerts/<id>", "Delete a legacy alert rule")
    _e("POST", "/api/tokens", "Create a scoped API token", body="JSON {name, scope, projects?} - projects limits a read/write token to project-filtered queue/history/audit reads")
    _e("DELETE", "/api/tokens/<id>", "Revoke a scoped API token")
    _e("GET", "/api/logs/<run_id>/stream", "SSE live log stream for a run")
    _e("GET", "/api/bundle/<project>/<hash>", "Download worker bundle tarball")
    _e("GET", "/api/package/<project>/<hash>", "Download code package overlay")
    _e("GET", "/api/transfer/get/<project>/<path>", "Worker transfer: download file")
    _e("GET", "/api/data/manifest/<project>", "Data manifest for project")
    _e("GET", "/api/data/file/<project>/<path>", "Data file download")
    _e("POST", "/api/project/pause/<project>", "Pause a project (scheduler skips its queued tasks; audited)")
    _e("POST", "/api/project/resume/<project>", "Resume a paused project (audited)")
    _e("POST", "/api/remove/<project>/<task_id>", "Unregister a task (ownership-checked; run history kept; audited)", params={"operator": "Caller identity", "force": "1 to override ownership"})
    _e("POST", "/api/project/remove/<project>", "Delete a project's tasks and runs; refused while tasks run unless force=1 (audited)", params={"force": "1 to remove while tasks run"})
    _e("GET", "/api/tasks/<project>/<task_id>/why", "Why a task is (not) running: scheduler block reason, deps, pause, attempts, dry-run verdict")
    _e("GET", "/api/data/blob/<sha256>", "Content-addressed data blob (worker; run token required)")
    _e("GET", "/api/data/snapshot/<run_id>/<rel_path>", "Consistent SQLite snapshot of a data file (worker; run token required)")
    _e("POST", "/api/data/push/<run_id>/<rel_path>", "Upload a content-addressed output blob (worker; run token required)")
    _e("POST", "/api/data/upload/<project>/<path>", "Upload a data file into a project's data directory")
    _e("GET", "/api/service/<name>/<subpath>", "Proxy a GET to a declared service (requires_services broker)")
    _e("POST", "/api/service/<name>/<subpath>", "Proxy a POST to a declared service (requires_services broker)")
    _e("GET", "/api/worker/commands", "Worker long-poll for commands (stop, drain, upgrade)", params={"worker_id": "Worker id", "session_id": "Session from register", "wait": "Long-poll seconds (max 25)"})
    _e("POST", "/api/worker/rotate-credential", "Worker replaces its credential hash (old hash and active session required)")
    _e("POST", "/api/project/stop-all/<project>", "Stop every running task of a project (audited)")
    _e("GET", "/api/runs/<run_id>/history", "Resource history samples for a run")
    _e("GET", "/api/worker/projects", "Worker: project config snapshot")
    _e("GET", "/api/sync/fingerprint/<project>", "Allowlisted tree fingerprint for worker file sync")
    _e("GET", "/api/sync/manifest/<project>", "Allowlisted file manifest for worker file sync")
    _e("GET", "/api/sync/file/<project>/<path>", "Download one allowlisted file for worker sync")
    _e("POST", "/api/groups/<name>/stop", "Stop running tasks in a group (?project= required for the ungrouped/empty name; ?dry_run=1)")
    _e("POST", "/api/groups/<name>/retry", "Retry failed tasks in a group (?project= required for ungrouped; ?completed=1&confirm=<N> = restart-all incl. completed, N from ?dry_run=1)")
    _e("POST", "/api/suites/<name>/retry", "Retry all tasks in a suite")
    _e("POST", "/api/cli/archive", "Archive old finished tasks: export to <home>/archive/<project>.jsonl, move YAML to queue_dir/archive/, drop DB rows (body: projects, older_than_days=30, statuses=[completed], dry_run)")
    _e("POST", "/api/cli/sync", "CLI sync trigger via HTTP (mtime-cached; optional body projects=[names], force=bool to re-parse all)")
    _e("POST", "/api/cli/add", "CLI add task via HTTP")
    _e("POST", "/api/notify/dead/<id>/retry", "Replay one dead-lettered webhook (single attempt); deleted on success")
    _e("POST", "/api/validate", "Dry-run validate task YAML (project, yaml_text); nothing is written")
    _e("POST", "/api/yield-master", "Release master lease")
    _e("POST", "/api/worker/deregister", "Worker deregister")
    _e("POST", "/api/worker/sync/<worker_id>", "Queue git pull + uv sync on worker")
    _e("POST", "/api/worker/file-sync/<worker_id>", "Queue allowlisted file sync on worker")
    _e("POST", "/api/transfer/put/<project>/<path>", "Worker transfer: upload file")
    _e("POST", "/api/mark-processed/<project>/<task_id>", "Mark notification processed")


def _build_api_docs_html() -> str:
    """Render /api/docs as a self-contained HTML page (no CDN)."""
    global _api_docs_html_cache
    if _api_docs_html_cache is not None:
        return _api_docs_html_cache
    rows = []
    methods_seen: set[str] = set()
    for e in _API_REGISTRY:
        m = e["method"]
        methods_seen.add(m)
        colour = {"GET": "#2a7", "POST": "#a72", "PATCH": "#75a", "DELETE": "#a22"}.get(m, "#555")
        params_html = ""
        if e.get("params"):
            prows = "".join(
                f"<tr><td><code>{k}</code></td><td>{v}</td></tr>"
                for k, v in e["params"].items()
            )
            params_html = f"<table class='params'><tr><th>Param</th><th>Description</th></tr>{prows}</table>"
        body_html = f"<p class='body'>Body: {e['body']}</p>" if e.get("body") else ""
        ret_html = f"<p class='ret'>Returns: {e['returns']}</p>" if e.get("returns") else ""
        auth_badge = "" if e.get("auth") else "<span class='no-auth'>no auth</span>"
        rows.append(
            f"<tr>"
            f"<td><span class='method' style='background:{colour}'>{m}</span></td>"
            f"<td><code>{e['path']}</code> {auth_badge}</td>"
            f"<td>{e['summary']}{params_html}{body_html}{ret_html}</td>"
            f"</tr>"
        )
    table = "\n".join(rows)
    from taskqueue import __version__
    html = f"""<!doctype html><html><head><meta charset=utf-8>
<title>taskqueue API v{__version__}</title>
<style>
body{{font-family:monospace;margin:2rem;background:#1a1a1a;color:#ddd}}
h1{{color:#eee}}table{{border-collapse:collapse;width:100%}}
th,td{{text-align:left;padding:.4rem .7rem;border-bottom:1px solid #333}}
th{{background:#2a2a2a;color:#aaa}}.method{{padding:.2rem .5rem;border-radius:3px;
color:#fff;font-weight:bold;font-size:.85em}}.params{{margin:.3rem 0 0 0;font-size:.85em}}
.params th,.params td{{border-color:#444;padding:.2rem .4rem}}code{{color:#8df}}
.body,.ret{{margin:.2rem 0;font-size:.85em;color:#aaa}}.no-auth{{color:#a72;font-size:.8em}}
</style></head><body>
<h1>taskqueue API</h1>
<p>Version: <code>{__version__}</code> &nbsp; Schema: <code>{SCHEMA_VERSION}</code></p>
<p><a href="/docs/errors?format=json">Error codes</a> · <a href="/docs/security?format=json">Security</a></p>
<table><tr><th>Method</th><th>Path</th><th>Description</th></tr>
{table}
</table></body></html>"""
    _api_docs_html_cache = html
    return html


_register_api_docs()
