"""Prometheus text exposition (version 0.0.4) for ``GET /api/metrics``."""

from __future__ import annotations

import sqlite3
from typing import Any

from taskqueue.queries import status_counts


def _esc(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\n").replace('"', '\\"')


def _sample(name: str, value: float | int, **labels: Any) -> str:
    if labels:
        body = ",".join(f'{k}="{_esc(v)}"' for k, v in sorted(labels.items()))
        return f"{name}{{{body}}} {value}"
    return f"{name} {value}"


def render_metrics(conn: sqlite3.Connection, *, daemon_rss_mb: float | None = None) -> str:
    """Render queue, worker and daemon gauges as Prometheus text."""
    lines: list[str] = []

    def family(name: str, help_text: str, samples: list[str]) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} gauge")
        lines.extend(samples)

    counts = status_counts(conn)
    family(
        "tq_tasks",
        "Tasks by status.",
        [_sample("tq_tasks", n, status=s) for s, n in sorted(counts.items())],
    )

    per_project = conn.execute(
        "SELECT project, COUNT(*) AS n FROM tasks WHERE status = 'running' GROUP BY project"
    ).fetchall()
    family(
        "tq_running_tasks",
        "Running tasks per project.",
        [_sample("tq_running_tasks", int(r["n"]), project=r["project"]) for r in per_project],
    )

    from taskqueue._db_runs import worker_health

    health: dict[str, int] = {}
    for r in conn.execute("SELECT last_seen FROM workers").fetchall():
        h = worker_health(r["last_seen"])
        health[h] = health.get(h, 0) + 1
    family(
        "tq_workers",
        "Remote workers by liveness state.",
        [_sample("tq_workers", n, state=s) for s, n in sorted(health.items())],
    )

    if daemon_rss_mb is not None:
        family(
            "tq_daemon_rss_megabytes",
            "Resident memory of the daemon process.",
            [_sample("tq_daemon_rss_megabytes", daemon_rss_mb)],
        )
    return "\n".join(lines) + "\n"
