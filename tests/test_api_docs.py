"""API documentation registry coverage."""

from __future__ import annotations

from taskqueue.web import _API_REGISTRY

# Exact GET/POST paths from web.py dispatch table (must appear in registry).
REQUIRED_PATHS = [
    "/api/status",
    "/api/dashboard",
    "/api/queue",
    "/api/tags",
    "/api/history",
    "/api/config",
    "/api/groups",
    "/api/suites",
    "/api/projects",
    "/api/project-limits",
    "/api/locks",
    "/api/project-roots",
    "/api/workers",
    "/api/attention",
    "/api/capacity",
    "/api/report",
    "/api/diagnose",
    "/api/cost",
    "/api/deps",
    "/api/hardware",
    "/api/usage",
    "/api/task-stats",
    "/api/timeline",
    "/api/schedules",
    "/api/alerts",
    "/api/alerts/system",
    "/api/audit",
    "/api/tokens",
    "/api/notifications",
    "/api/events",
    "/api/docs",
    "/api/docs.json",
    "/api/help",
    "/api/tasks",
    "/api/worker/projects",
    "/api/worker/version",
    "/api/worker/wheel",
    "/api/worker/next-task",
    "/api/stop/<project>/<task_id>",
    "/api/retry/<project>/<task_id>",
    "/api/enable/<project>/<task_id>",
    "/api/priority/<project>/<task_id>/<N>",
    "/api/project/freeze/<project>",
    "/api/project/unfreeze/<project>",
    "/api/force/<project>/<task_id>",
    "/api/worker/register",
    "/api/worker/heartbeat",
    "/api/worker/finalize",
    "/api/worker/log-chunk",
    "/api/groups/<name>/stop",
    "/api/groups/<name>/retry",
    "/api/suites/<name>/retry",
    "/api/logs/<run_id>",
    "/api/logs/<run_id>/stream",
    "/api/bundle/<project>/<hash>",
    "/api/cli/sync",
    "/api/cli/add",
    "/api/yield-master",
    "/api/alerts/<id>",
    "/api/alerts/system/<key>",
    "/api/tokens/<id>",
]


def _registry_paths() -> set[str]:
    return {e["path"] for e in _API_REGISTRY}


def test_api_registry_covers_required_routes():
    reg = _registry_paths()
    missing = [p for p in REQUIRED_PATHS if p not in reg]
    assert not missing, f"undocumented routes: {missing}"


def test_api_registry_non_empty():
    assert len(_API_REGISTRY) >= len(REQUIRED_PATHS)


def _dispatched_routes() -> tuple[set[str], set[str]]:
    """Exact paths and path prefixes the handler dispatches (read from its route tables)."""
    import re
    from pathlib import Path

    import taskqueue.web.handler as h

    src = Path(h.__file__).read_text(encoding="utf-8")
    exact = set(re.findall(r'"(/api/[^"<>]*)":\s*(?:lambda|self\.)', src))
    prefixes = set(re.findall(r'path\.startswith\("(/api/[^"]+/)"\)', src))
    return exact, prefixes


def test_every_dispatched_route_is_documented():
    reg = _registry_paths()
    exact, prefixes = _dispatched_routes()
    missing = sorted(p for p in exact if p not in reg)
    missing += sorted(p + "…" for p in prefixes if not any(r.startswith(p) for r in reg))
    assert not missing, f"routes without an api_docs entry: {missing}"
