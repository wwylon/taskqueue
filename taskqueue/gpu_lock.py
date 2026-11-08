"""F-241: ``~/.taskqueue/locks/gpu.lock`` while a GPU task runs on this host.

Used by local pytest (and similar) to skip GPU tiers deterministically. Keyed on
``task_requires_gpu`` (``gpu_vram_gib > 0``). The file is present while at least
one holder is recorded; JSON top-level fields describe one current holder.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from taskqueue.config import gpu_lock_path

_lock = threading.Lock()


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but not ours
    except OSError:
        # Windows: os.kill(pid, 0) may raise OSError for a dead pid.
        try:
            import psutil

            return psutil.pid_exists(pid)
        except Exception:  # noqa: BLE001 - best-effort
            return False


def _read_holders(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError, TypeError):
        return []
    if not isinstance(data, dict):
        return []
    holders = data.get("holders")
    if isinstance(holders, list):
        return [h for h in holders if isinstance(h, dict)]
    # Legacy / single-holder shape.
    if data.get("project") and data.get("task_id") is not None:
        return [data]
    return []


def _write_holders(path: Path, holders: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not holders:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return
    top = dict(holders[0])
    top["holders"] = holders
    path.write_text(json.dumps(top, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _holder_key(h: dict[str, Any]) -> tuple:
    return (str(h.get("project")), str(h.get("task_id")), int(h.get("run_id") or 0))


def acquire(
    home: Path,
    *,
    project: str,
    task_id: str,
    run_id: int,
    pid: int,
    started: float | None = None,
) -> Path:
    """Record a GPU holder and ensure ``gpu.lock`` exists. Returns the lock path."""
    path = gpu_lock_path(home)
    entry = {
        "project": project,
        "task_id": task_id,
        "run_id": int(run_id),
        "pid": int(pid),
        "started": float(started if started is not None else time.time()),
    }
    with _lock:
        holders = _read_holders(path)
        holders = [h for h in holders if _holder_key(h) != _holder_key(entry)]
        holders.append(entry)
        _write_holders(path, holders)
    return path


def release(home: Path, *, project: str, task_id: str, run_id: int) -> None:
    """Drop one holder; remove the file when none remain."""
    path = gpu_lock_path(home)
    key = (str(project), str(task_id), int(run_id))
    with _lock:
        holders = [h for h in _read_holders(path) if _holder_key(h) != key]
        _write_holders(path, holders)


def scrub_stale(home: Path) -> int:
    """Remove holders whose pid is gone. Returns number of holders removed."""
    path = gpu_lock_path(home)
    with _lock:
        holders = _read_holders(path)
        keep = [h for h in holders if _pid_alive(int(h.get("pid") or 0))]
        removed = len(holders) - len(keep)
        _write_holders(path, keep)
    return removed


def sync_from_holders(home: Path, holders: list[dict[str, Any]]) -> None:
    """Replace the lock file contents with ``holders`` (empty → delete)."""
    path = gpu_lock_path(home)
    cleaned: list[dict[str, Any]] = []
    for h in holders:
        if not isinstance(h, dict):
            continue
        cleaned.append(
            {
                "project": str(h.get("project") or ""),
                "task_id": str(h.get("task_id") or ""),
                "run_id": int(h.get("run_id") or 0),
                "pid": int(h.get("pid") or 0),
                "started": float(h.get("started") or time.time()),
            }
        )
    with _lock:
        _write_holders(path, cleaned)


def holders_from_active_runs(active_runs: dict) -> list[dict[str, Any]]:
    """Build holder dicts from local ``ActiveRun`` values that require a GPU."""
    from taskqueue.resources import task_requires_gpu

    out: list[dict[str, Any]] = []
    for run in active_runs.values():
        row = getattr(run, "task_row", None)
        resources = row["resources"] if row is not None else None
        if not task_requires_gpu(resources):
            continue
        proc = getattr(run, "proc", None)
        pid = int(proc.pid) if proc is not None and getattr(proc, "pid", None) else 0
        out.append(
            {
                "project": getattr(run, "project", ""),
                "task_id": getattr(run, "task_id", ""),
                "run_id": int(getattr(run, "run_id", 0) or 0),
                "pid": pid,
                "started": float(getattr(run, "started_at", 0.0) or time.time()),
            }
        )
    return out
