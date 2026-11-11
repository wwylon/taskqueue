"""Health checks behind `tq doctor` (and `--json`): each check is ok, warn or fail.

A ``fail`` makes `tq doctor` exit 1. Checks are read-only: the DB is opened with
``mode=ro`` so a running daemon is never migrated or locked by the doctor.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any

WAL_WARN_BYTES = 512 * 1024 * 1024
DISK_WARN_BYTES = 2 * 1024**3
DISK_FAIL_BYTES = 200 * 1024**2


def _check(name: str, level: str, detail: str) -> dict[str, str]:
    return {"name": name, "level": level, "detail": detail}


def _check_legacy_limits(db: Path, checks: list[dict[str, str]]) -> None:
    """Advise on ``max_concurrent_tasks`` (Wave 4.7): it overlaps ``max_running_tasks``."""
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5.0)
    try:
        row = conn.execute("SELECT value FROM config WHERE key = 'max_concurrent_tasks'").fetchone()
    except sqlite3.Error:
        return
    finally:
        conn.close()
    try:
        value = int(json.loads(row[0])) if row else 0
    except (TypeError, ValueError):
        value = 0
    if value > 0:
        checks.append(
            _check(
                "limits",
                "warn",
                f"max_concurrent_tasks={value} is legacy (force bypasses it, excess is never stopped); "
                f"use: tq config set max_running_tasks={value} max_concurrent_tasks=0",
            )
        )


def run_checks(home: Path) -> dict[str, Any]:
    from tqdaemon._cli_helpers import _daemon_running, _pid_path
    from tqdaemon.config import db_path, load_projects
    from tqdaemon.master_client import api_request

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
        checks.append(
            _check("daemon", "ok" if http_ok else "fail", f"pid {pid}, http {'ok' if http_ok else 'not answering'}")
        )

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
            _check_legacy_limits(db, checks)
        except sqlite3.Error as exc:
            checks.append(_check("database", "fail", f"cannot read: {exc}"))
        wal = db.with_name(db.name + "-wal")
        if wal.is_file():
            size = wal.stat().st_size
            checks.append(_check("wal", "warn" if size > WAL_WARN_BYTES else "ok", f"{size / 1024**2:.1f} MiB"))

    # Free disk at the state home
    try:
        free = shutil.disk_usage(home if home.exists() else home.parent).free
        level = "fail" if free < DISK_FAIL_BYTES else "warn" if free < DISK_WARN_BYTES else "ok"
        checks.append(_check("disk", level, f"{free / 1024**3:.1f} GiB free"))
    except OSError as exc:
        checks.append(_check("disk", "warn", f"unknown: {exc}"))

    # Registered projects: roots and queue dirs exist
    try:
        projects = load_projects(home)
        missing = [f"{n}: root {p.root}" for n, p in projects.items() if not Path(p.root).is_dir()]
        missing += [
            f"{n}: queue_dir {p.queue_dir}"
            for n, p in projects.items()
            if Path(p.root).is_dir() and not Path(p.queue_dir).is_dir()
        ]
        checks.append(
            _check(
                "projects",
                "warn" if missing else "ok",
                "; ".join(missing) if missing else f"{len(projects)} registered",
            )
        )
    except Exception as exc:  # noqa: BLE001 - a malformed projects.yaml is a failed check; malformed projects.yaml
        checks.append(_check("projects", "fail", f"projects.yaml: {exc}"))

    from tqdaemon.config import env_home_value

    _env_home = env_home_value()
    if _env_home and Path(_env_home) != home:
        checks.append(_check("home", "warn", "TQDAEMON_HOME differs from the home in use"))

    try:
        from tqdaemon.yaml_sync import yaml_cache_stats

        stats = yaml_cache_stats()
        checks.append(
            _check(
                "yaml_cache",
                "ok",
                f"{stats['entries']} entries, {stats['bytes']} bytes "
                f"(budget {stats['max_bytes']} bytes), hits={stats['hits']}, misses={stats['misses']}",
            )
        )
    except Exception as exc:  # noqa: BLE001 - one doctor check failing must not hide the rest
        checks.append(_check("yaml_cache", "warn", f"unavailable: {exc}"))

    worst = (
        "fail"
        if any(c["level"] == "fail" for c in checks)
        else ("warn" if any(c["level"] == "warn" for c in checks) else "ok")
    )
    return {"ok": worst != "fail", "status": worst, "checks": checks, "home": str(home)}


def propose_remediations(home: Path, checks: list[dict[str, str]] | None = None) -> list[dict[str, str]]:
    """F-215 / C-004: read-only remediation proposals for ``tq doctor --fix``.

    Never writes SQLite or deletes files. Each item has ``id``, ``summary``, and
    usually a ``command`` the operator can run after review.
    """
    from tqdaemon._cli_helpers import _ENABLED_NEEDS_RETRY_STATUSES, _pid_path
    from tqdaemon.config import db_path

    home = Path(home)
    if checks is None:
        checks = run_checks(home)["checks"]
    by_name = {c["name"]: c for c in checks}
    out: list[dict[str, str]] = []

    daemon = by_name.get("daemon")
    if daemon and daemon["level"] == "warn" and "not running" in daemon["detail"]:
        out.append(
            {
                "id": "start_daemon",
                "summary": "Daemon is not running",
                "command": "tq start",
            }
        )
    if daemon and daemon["level"] == "fail":
        out.append(
            {
                "id": "restart_daemon",
                "summary": "Daemon process up but HTTP not answering",
                "command": "tq restart --force",
            }
        )
    pid_file = by_name.get("pid_file")
    if pid_file and pid_file["level"] == "warn":
        out.append(
            {
                "id": "remove_stale_pid",
                "summary": f"Stale pid file at {_pid_path(home)}",
                "command": f"Remove-Item -Force {_pid_path(home)}" if os.name == "nt" else f"rm -f {_pid_path(home)}",
            }
        )
    wal = by_name.get("wal")
    if wal and wal["level"] == "warn":
        out.append(
            {
                "id": "checkpoint_wal",
                "summary": f"WAL is large ({wal['detail']})",
                "command": "tq prune  # review dry-run; then tq prune --apply if OK",
            }
        )
    disk = by_name.get("disk")
    if disk and disk["level"] in ("warn", "fail"):
        out.append(
            {
                "id": "free_disk",
                "summary": f"Low free disk ({disk['detail']})",
                "command": "tq prune  # dry-run default; tq prune --apply after review",
            }
        )
    projects = by_name.get("projects")
    if projects and projects["level"] == "warn" and "registered" not in projects["detail"]:
        out.append(
            {
                "id": "fix_projects_yaml",
                "summary": f"Project path problems: {projects['detail']}",
                "command": "Edit projects.yaml (or re-register roots) then tq sync",
            }
        )

    db = db_path(home)
    if db.is_file():
        try:
            conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5.0)
            conn.row_factory = sqlite3.Row
            try:
                statuses = tuple(sorted(_ENABLED_NEEDS_RETRY_STATUSES))
                placeholders = ",".join("?" * len(statuses))
                rows = conn.execute(
                    f"SELECT project, COUNT(*) AS n FROM tasks "
                    f"WHERE enabled = 1 AND status IN ({placeholders}) "
                    f"GROUP BY project ORDER BY n DESC LIMIT 12",
                    statuses,
                ).fetchall()
                for row in rows:
                    proj = str(row["project"])
                    n = int(row["n"])
                    out.append(
                        {
                            "id": f"retry_enabled_{proj}",
                            "summary": f"{n} enabled terminal task(s) in {proj} need tq retry",
                            "command": (
                                f"tq retry --project {proj} --status {','.join(sorted(_ENABLED_NEEDS_RETRY_STATUSES))}"
                            ),
                        }
                    )
                offline = conn.execute(
                    "SELECT worker_id, last_seen FROM workers WHERE status != 'online' ORDER BY last_seen DESC LIMIT 8"
                ).fetchall()
                for w in offline:
                    wid = str(w["worker_id"])
                    out.append(
                        {
                            "id": f"worker_offline_{wid}",
                            "summary": f"Worker {wid} is offline (last_seen={w['last_seen']})",
                            "command": "Re-attach with tq worker start (see docs/WORKER_SETUP.md)",
                        }
                    )
            finally:
                conn.close()
        except sqlite3.Error:
            pass

    return out
