"""Shared run metrics for UI / CLI / MCP parity."""

from __future__ import annotations
from taskqueue._time import parse_iso as _parse_iso

import json
import sqlite3
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _round_or_none(v: Any, nd: int = 2) -> float | None:
    if v is None:
        return None
    try:
        return round(float(v), nd)
    except (TypeError, ValueError):
        return None


def _elapsed_eta(started_at: str | None, progress_pct: float | None) -> tuple[float | None, float | None]:
    elapsed_sec = None
    eta_sec = None
    if not started_at:
        return None, None
    try:
        started = _parse_iso(started_at)
        elapsed_sec = round((datetime.now(UTC) - started).total_seconds(), 1)
        if progress_pct and progress_pct > 0 and elapsed_sec:
            eta_sec = round(elapsed_sec / progress_pct * (100.0 - progress_pct), 1)
    except Exception:
        pass
    return elapsed_sec, eta_sec


def _history_rows(conn: sqlite3.Connection, run_id: int, samples: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT ts, cpu_pct, ram_gib, vram_gib, gpu_util_pct FROM resource_history "
        "WHERE run_id = ? ORDER BY ts DESC LIMIT ?",
        (run_id, max(1, samples)),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in reversed(rows):
        keys = r.keys()
        out.append({
            "ts": r["ts"],
            "cpu_pct": _round_or_none(r["cpu_pct"], 1),
            "ram_gib": _round_or_none(r["ram_gib"], 2),
            "vram_gib": _round_or_none(r["vram_gib"], 2),
            "gpu_util_pct": _round_or_none(r["gpu_util_pct"], 1)
            if "gpu_util_pct" in keys else None,
        })
    return out


def _summary(history: list[dict[str, Any]]) -> dict[str, Any]:
    def _agg(key: str) -> dict[str, float] | None:
        vals = [float(h[key]) for h in history if h.get(key) is not None]
        if not vals:
            return None
        return {"avg": round(sum(vals) / len(vals), 2), "max": round(max(vals), 2)}

    return {
        "n": len(history),
        "cpu_pct": _agg("cpu_pct"),
        "ram_gib": _agg("ram_gib"),
        "vram_gib": _agg("vram_gib"),
        "gpu_util_pct": _agg("gpu_util_pct"),
    }


def _try_http_overlay(home: Path, project: str, task_id: str) -> dict[str, Any] | None:
    """Best-effort live currents from the daemon HTTP API (same as the UI)."""
    try:
        from taskqueue.db import get_config, open_db
        from taskqueue import config as tqconfig

        db = open_db(tqconfig.db_path(home))
        try:
            cfg = get_config(db)
        finally:
            db.close()
        port = int(cfg.get("port") or 8765)
        token = str(cfg.get("auth_token") or "")
        url = f"http://127.0.0.1:{port}/api/status"
        req = urllib.request.Request(url)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        for t in data.get("running") or []:
            if t.get("project") == project and t.get("task_id") == task_id:
                return t
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, json.JSONDecodeError):
        return None
    return None


def build_run_metrics(
    conn: sqlite3.Connection,
    project: str,
    task_id: str,
    *,
    home: Path | None = None,
    samples: int = 60,
    include_history: bool = False,
) -> dict[str, Any]:
    """Return the metrics payload shared by MCP, CLI, and (conceptually) the UI."""
    from taskqueue import config as tqconfig

    home = home or tqconfig.state_home()
    task = conn.execute(
        "SELECT status, resources FROM tasks WHERE project = ? AND task_id = ?",
        (project, task_id),
    ).fetchone()
    run = conn.execute(
        "SELECT * FROM runs WHERE project = ? AND task_id = ? ORDER BY run_id DESC LIMIT 1",
        (project, task_id),
    ).fetchone()

    status = task["status"] if task else None
    run_id = run["run_id"] if run else None
    progress_pct = run["progress_pct"] if run else None
    last_line = run["last_log_line"] if run else None
    started_at = run["started_at"] if run else None
    elapsed_sec, eta_sec = _elapsed_eta(started_at, progress_pct)

    peak = {
        "cpu_pct": _round_or_none(run["peak_cpu_pct"], 1) if run else None,
        "ram_gib": _round_or_none(run["peak_ram_gib"], 2) if run else None,
        "vram_gib": _round_or_none(run["peak_vram_gib"], 2) if run else None,
    }

    history = _history_rows(conn, int(run_id), samples) if run_id else []
    last = history[-1] if history else {}
    current = {
        "cpu_pct": last.get("cpu_pct"),
        "ram_gib": last.get("ram_gib"),
        "vram_gib": last.get("vram_gib"),
        "gpu_util_pct": last.get("gpu_util_pct"),
    }
    summary = _summary(history)
    avg_gpu = None
    if summary.get("gpu_util_pct"):
        avg_gpu = summary["gpu_util_pct"]["avg"]

    source = "db"
    live = _try_http_overlay(home, project, task_id)
    if live:
        source = "db+http"
        for k in ("cpu_pct", "ram_gib", "vram_gib", "gpu_util_pct"):
            # status API uses current_cpu_pct etc.; gpu uses current_gpu_util_pct / avg_gpu_util_pct
            mapping = {
                "cpu_pct": "current_cpu_pct",
                "ram_gib": "current_ram_gib",
                "vram_gib": "current_vram_gib",
                "gpu_util_pct": "current_gpu_util_pct",
            }
            v = live.get(mapping[k])
            if v is not None:
                current[k] = _round_or_none(v, 1 if "pct" in k else 2)
        if live.get("avg_gpu_util_pct") is not None:
            avg_gpu = _round_or_none(live["avg_gpu_util_pct"], 1)
        if live.get("progress_pct") is not None:
            progress_pct = live["progress_pct"]
        if live.get("last_log_line") is not None:
            last_line = live["last_log_line"]
        if live.get("elapsed_sec") is not None:
            elapsed_sec = live["elapsed_sec"]
        if live.get("run_id") is not None:
            run_id = live["run_id"]
        for pk, sk in (
            ("cpu_pct", "peak_cpu_pct"),
            ("ram_gib", "peak_ram_gib"),
            ("vram_gib", "peak_vram_gib"),
        ):
            if live.get(sk) is not None:
                peak[pk] = _round_or_none(live[sk], 1 if "pct" in pk else 2)

    metrics_json = None
    if run and "metrics_json" in run.keys() and run["metrics_json"]:
        try:
            metrics_json = json.loads(run["metrics_json"])
        except Exception:
            metrics_json = None

    from taskqueue.log_issues import coerce_log_counts, log_from_metrics_json
    from taskqueue.resources import task_requires_gpu

    log_counts = log_from_metrics_json(metrics_json)
    if live and isinstance(live.get("log"), dict):
        log_counts = coerce_log_counts(live["log"])

    requires_gpu = task_requires_gpu(task["resources"] if task else None)

    out: dict[str, Any] = {
        "project": project,
        "task_id": task_id,
        "run_id": run_id,
        "status": status,
        "progress_pct": progress_pct,
        "eta_sec": eta_sec,
        "elapsed_sec": elapsed_sec,
        "last_line": last_line,
        "requires_gpu": requires_gpu,
        "log": log_counts,
        "resources": {
            "current": current,
            "peak": peak,
            "avg": {"gpu_util_pct": avg_gpu},
        },
        "history_summary": summary,
        "metrics": metrics_json,
        "source": source,
    }
    if include_history:
        out["history"] = history
    return out
