"""Health checks behind `tq doctor` (and `--json`): each check is ok, warn or fail.

A ``fail`` makes `tq doctor` exit 1. Checks are read-only: the DB is opened with
``mode=ro`` so a running daemon is never migrated or locked by the doctor.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any

WAL_WARN_BYTES = 512 * 1024 * 1024
DISK_WARN_BYTES = 2 * 1024 ** 3
DISK_FAIL_BYTES = 200 * 1024 ** 2


def _check(name: str, level: str, detail: str) -> dict[str, str]:
    return {"name": name, "level": level, "detail": detail}


def run_checks(home: Path) -> dict[str, Any]:
    from taskqueue._cli_helpers import _daemon_running, _pid_path
    from taskqueue.config import db_path, load_projects
    from taskqueue.master_client import api_request

    home = Path(home)
    checks: list[dict[str, str]] = []

    # Daemon process + HTTP
    pid = _daemon_running(home)
    pid_file = _pid_path(home)
    if pid is None:
        if pid_file.is_file():
            checks.append(_check("pid_file", "warn", f"stale {pid_file.name} (process not running)"))
        checks.append(_check("daemon", "warn", "not running (tq start)"))
    else:
        http_ok = api_request(home, "GET", "/api/status", timeout=3.0) is not None
        checks.append(_check("daemon", "ok" if http_ok else "fail",
                             f"pid {pid}, http {'ok' if http_ok else 'not answering'}"))

    # Database integrity and WAL size (read-only)
    db = db_path(home)
    if not db.is_file():
        checks.append(_check("database", "warn", f"no database at {db}"))
    else:
        try:
            conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5.0)
            try:
                res = conn.execute("PRAGMA quick_check").fetchone()[0]
            finally:
                conn.close()
            checks.append(_check("database", "ok" if res == "ok" else "fail", f"quick_check: {res}"))
        except sqlite3.Error as exc:
            checks.append(_check("database", "fail", f"cannot read: {exc}"))
        wal = db.with_name(db.name + "-wal")
        if wal.is_file():
            size = wal.stat().st_size
            checks.append(_check("wal", "warn" if size > WAL_WARN_BYTES else "ok",
                                 f"{size / 1024 ** 2:.1f} MiB"))

    # Free disk at the state home
    try:
        free = shutil.disk_usage(home if home.exists() else home.parent).free
        level = "fail" if free < DISK_FAIL_BYTES else "warn" if free < DISK_WARN_BYTES else "ok"
        checks.append(_check("disk", level, f"{free / 1024 ** 3:.1f} GiB free"))
    except OSError as exc:
        checks.append(_check("disk", "warn", f"unknown: {exc}"))

    # Registered projects: roots and queue dirs exist
    try:
        projects = load_projects(home)
        missing = [f"{n}: root {p.root}" for n, p in projects.items() if not Path(p.root).is_dir()]
        missing += [f"{n}: queue_dir {p.queue_dir}" for n, p in projects.items()
                    if Path(p.root).is_dir() and not Path(p.queue_dir).is_dir()]
        checks.append(_check("projects", "warn" if missing else "ok",
                             "; ".join(missing) if missing else f"{len(projects)} registered"))
    except Exception as exc:  # malformed projects.yaml
        checks.append(_check("projects", "fail", f"projects.yaml: {exc}"))

    if os.environ.get("TASKQUEUE_HOME") and Path(os.environ["TASKQUEUE_HOME"]) != home:
        checks.append(_check("home", "warn", "TASKQUEUE_HOME differs from the home in use"))

    worst = "fail" if any(c["level"] == "fail" for c in checks) else (
        "warn" if any(c["level"] == "warn" for c in checks) else "ok")
    return {"ok": worst != "fail", "status": worst, "checks": checks, "home": str(home)}
