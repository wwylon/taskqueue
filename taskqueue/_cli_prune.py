"""W4.7: Log and run-record retention — tq prune."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from taskqueue._time import _now as _now_iso

UTC = timezone.utc

# Default retention windows (days).
DEFAULT_LOG_DAYS = 90
DEFAULT_RUN_DAYS = 180
DEFAULT_AUDIT_DAYS = 365
# Byte-tail cap for finished logs (0 in config = off). Used when key is unset.
DEFAULT_LOG_MAX_BYTES = 1_048_576
_BATCH = 500
ROTATE_BYTES = 10 * 1024 * 1024


def _log_dirs(home: Path) -> list[Path]:
    logs_root = home / "logs"
    if not logs_root.is_dir():
        return []
    return [p for p in logs_root.iterdir() if p.is_dir()]


def _per_project_retention(config: dict[str, Any], project: str) -> tuple[int, int]:
    """Return (log_days, run_days) for a project, honouring per-project overrides."""
    global_log = int(config.get("retention_log_days", DEFAULT_LOG_DAYS))
    global_run = int(config.get("retention_run_days", DEFAULT_RUN_DAYS))
    overrides = config.get("retention_projects") or {}
    if isinstance(overrides, str):
        try:
            overrides = json.loads(overrides)
        except ValueError:
            overrides = {}
    if not isinstance(overrides, dict):
        overrides = {}
    proj_cfg = overrides.get(project)
    if not isinstance(proj_cfg, dict):
        proj_cfg = {}
    log_days = int(proj_cfg.get("log_days", global_log))
    run_days = int(proj_cfg.get("run_days", global_run))
    return log_days, run_days


def prune_logs(
    home: Path,
    config: dict[str, Any],
    *,
    dry_run: bool = True,
    verbose: bool = False,
    protected: set[str] | None = None,
) -> dict[str, Any]:
    """Delete log files older than retention_log_days. Returns summary dict.

    ``protected`` holds resolved log paths that must stay (active runs, ``tasks.last_run_id``).
    """
    now_ts = time.time()
    protected = protected or set()
    total_bytes = 0
    total_files = 0
    per_project: dict[str, dict] = {}

    for proj_dir in _log_dirs(home):
        project = proj_dir.name
        log_days, _ = _per_project_retention(config, project)
        cutoff = now_ts - log_days * 86400

        proj_bytes = 0
        proj_files = 0
        for f in proj_dir.rglob("*.log"):
            try:
                mtime = f.stat().st_mtime
                size = f.stat().st_size
            except OSError:
                continue
            if mtime < cutoff and str(f.resolve()) not in protected:
                if verbose:
                    age_days = (now_ts - mtime) / 86400
                    print(f"  {'(dry) ' if dry_run else ''}prune log {f}  ({age_days:.0f}d, {size//1024}KB)")
                if not dry_run:
                    try:
                        f.unlink()
                    except OSError:
                        pass
                proj_bytes += size
                proj_files += 1

        if proj_files:
            per_project[project] = {"files": proj_files, "bytes": proj_bytes, "log_days": log_days}
        total_bytes += proj_bytes
        total_files += proj_files

    return {"logs_deleted": total_files, "logs_bytes": total_bytes, "per_project": per_project}


def prune_runs(
    home: Path,
    conn,
    config: dict[str, Any],
    *,
    dry_run: bool = True,
    verbose: bool = False,
) -> dict[str, Any]:
    """Delete old run records from the DB. Returns summary dict."""
    now_ts = time.time()
    total_rows = 0

    projects_in_db = [
        r[0] for r in conn.execute("SELECT DISTINCT project FROM runs").fetchall()
    ]

    for project in projects_in_db:
        _, run_days = _per_project_retention(config, project)
        cutoff_iso = datetime.fromtimestamp(now_ts - run_days * 86400, UTC).isoformat()

        rows = conn.execute(
            "SELECT run_id FROM runs WHERE project = ? AND status IN ('completed','failed','interrupted') "
            "AND finished_at IS NOT NULL AND finished_at < ? "
            "AND run_id NOT IN (SELECT last_run_id FROM tasks WHERE last_run_id IS NOT NULL)",
            (project, cutoff_iso),
        ).fetchall()
        run_ids = [r[0] for r in rows]

        if verbose and run_ids:
            print(f"  {'(dry) ' if dry_run else ''}prune {len(run_ids)} runs for {project}  (>{run_days}d)")

        if not dry_run:
            for i in range(0, len(run_ids), _BATCH):
                chunk = run_ids[i : i + _BATCH]
                placeholders = ",".join("?" * len(chunk))
                conn.execute(f"DELETE FROM resource_history WHERE run_id IN ({placeholders})", chunk)
                conn.execute(f"DELETE FROM runs WHERE run_id IN ({placeholders})", chunk)
                conn.commit()

        total_rows += len(run_ids)

    return {"runs_deleted": total_rows}


def prune_audit(conn, config: dict[str, Any], *, dry_run: bool = True) -> dict[str, Any]:
    """Delete operator_audit rows older than ``retention_audit_days``."""
    days = max(1, int(config.get("retention_audit_days", DEFAULT_AUDIT_DAYS)))
    cutoff = datetime.fromtimestamp(time.time() - days * 86400, UTC).isoformat()
    if dry_run:
        n = conn.execute("SELECT COUNT(*) FROM operator_audit WHERE ts < ?", (cutoff,)).fetchone()[0]
    else:
        n = conn.execute("DELETE FROM operator_audit WHERE ts < ?", (cutoff,)).rowcount or 0
        conn.commit()
    return {"audit_deleted": int(n)}


def rotate_logs(home: Path, *, max_bytes: int = ROTATE_BYTES, dry_run: bool = True) -> list[str]:
    """Rotate oversized append-only logs to ``<name>.1`` (readers reset their byte cursor)."""
    rotated: list[str] = []
    for name in ("notify.jsonl", "notify_failed.jsonl", "prune_log.jsonl"):
        p = home / name
        try:
            if p.is_file() and p.stat().st_size > max_bytes:
                if not dry_run:
                    p.replace(p.with_name(name + ".1"))
                rotated.append(name)
        except OSError:
            continue
    return rotated


def _protected_logs(conn) -> set[str]:
    """Log paths that must survive pruning: unfinished runs and each task's latest run."""
    out: set[str] = set()
    rows = conn.execute(
        "SELECT log_path FROM runs WHERE log_path IS NOT NULL AND "
        "(finished_at IS NULL OR run_id IN (SELECT last_run_id FROM tasks WHERE last_run_id IS NOT NULL))"
    ).fetchall()
    for (lp,) in rows:
        try:
            out.add(str(Path(lp).resolve()))
        except OSError:
            continue
    return out


def _unfinished_logs(conn) -> set[str]:
    """Log paths still being written (finished_at IS NULL) — must not be capped."""
    out: set[str] = set()
    rows = conn.execute(
        "SELECT log_path FROM runs WHERE log_path IS NOT NULL AND finished_at IS NULL"
    ).fetchall()
    for (lp,) in rows:
        try:
            out.add(str(Path(lp).resolve()))
        except OSError:
            continue
    return out


def _log_max_bytes(config: dict[str, Any]) -> int:
    """Return configured cap; missing key → DEFAULT_LOG_MAX_BYTES; explicit 0 → off."""
    if "retention_log_max_bytes" not in config:
        return DEFAULT_LOG_MAX_BYTES
    try:
        return max(0, int(config["retention_log_max_bytes"]))
    except (TypeError, ValueError):
        return DEFAULT_LOG_MAX_BYTES


def _read_log_tail_bytes(path: Path, max_bytes: int) -> bytes:
    """Last max_bytes of path, aligned to the next newline when truncated."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        if size <= max_bytes:
            f.seek(0)
            return f.read()
        f.seek(size - max_bytes)
        data = f.read(max_bytes)
    nl = data.find(b"\n")
    if nl != -1:
        data = data[nl + 1 :]
    return data


def cap_finished_logs(
    home: Path,
    config: dict[str, Any],
    *,
    dry_run: bool = True,
    verbose: bool = False,
    unfinished: set[str] | None = None,
) -> dict[str, Any]:
    """Rewrite finished .log files larger than retention_log_max_bytes to a byte tail.

    Preserves mtime so age-based prune is not delayed. Skips unfinished (writing) logs.
    Latest-run logs are capped (they are protected from deletion, not from capping).
    """
    max_bytes = _log_max_bytes(config)
    if max_bytes <= 0:
        return {"logs_capped": 0, "logs_bytes_reclaimed": 0}

    unfinished = unfinished or set()
    total_files = 0
    total_reclaimed = 0

    for proj_dir in _log_dirs(home):
        for f in proj_dir.rglob("*.log"):
            try:
                resolved = str(f.resolve())
                st = f.stat()
            except OSError:
                continue
            if resolved in unfinished or st.st_size <= max_bytes:
                continue
            reclaimed = st.st_size - max_bytes
            if verbose:
                print(
                    f"  {'(dry) ' if dry_run else ''}cap log {f}  "
                    f"({st.st_size // 1024}KB -> <={max_bytes // 1024}KB)"
                )
            if not dry_run:
                try:
                    data = _read_log_tail_bytes(f, max_bytes)
                    atime, mtime = st.st_atime, st.st_mtime
                    tmp = f.with_name(f.name + ".tqcap.tmp")
                    tmp.write_bytes(data)
                    tmp.replace(f)
                    os.utime(f, (atime, mtime))
                    reclaimed = st.st_size - f.stat().st_size
                except OSError:
                    continue
            total_files += 1
            total_reclaimed += max(0, reclaimed)

    return {"logs_capped": total_files, "logs_bytes_reclaimed": total_reclaimed}


def run_maintenance(home: Path, conn, config: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
    """Retention pass used by the daemon: age-delete logs, cap finished tails, runs, audit, rotate."""
    logs = prune_logs(home, config, dry_run=dry_run, protected=_protected_logs(conn))
    capped = cap_finished_logs(home, config, dry_run=dry_run, unfinished=_unfinished_logs(conn))
    runs = prune_runs(home, conn, config, dry_run=dry_run)
    audit = prune_audit(conn, config, dry_run=dry_run)
    rotated = rotate_logs(home, dry_run=dry_run)
    result = {**logs, **capped, **runs, **audit, "rotated": rotated}
    if not dry_run and (runs["runs_deleted"] or audit["audit_deleted"]):
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("PRAGMA incremental_vacuum")  # no-op unless auto_vacuum=INCREMENTAL
        except Exception:  # best effort; readers may hold the WAL open
            pass
    return result


def cmd_prune(home: Path, dry_run: bool = True, verbose: bool = False) -> int:
    from taskqueue.config import db_path
    from taskqueue.db import get_config, open_db

    db_file = db_path(home)
    if not db_file.exists():
        print("tq prune: no database found.")
        return 1

    conn = open_db(db_file)
    config = get_config(conn)

    if not config.get("retention_enabled"):
        print("Retention is disabled. Enable with: tq config set retention_enabled true")
        print("(--dry-run is always safe; configure then re-run without --dry-run to apply)")
        if not dry_run:
            conn.close()
            return 1

    mode = "DRY RUN — no files or records will be changed" if dry_run else "LIVE — deleting files and DB records"
    print(f"tq prune  [{mode}]")
    log_days_default = int(config.get("retention_log_days", DEFAULT_LOG_DAYS))
    run_days_default = int(config.get("retention_run_days", DEFAULT_RUN_DAYS))
    max_bytes = _log_max_bytes(config)
    cap_note = f", cap <={max_bytes}B" if max_bytes > 0 else ", cap off"
    print(f"  defaults: logs >{log_days_default}d, runs >{run_days_default}d{cap_note}")

    # Age-delete first (mtime-based), then cap remaining finished logs (mtime preserved).
    log_result = prune_logs(home, config, dry_run=dry_run, verbose=verbose, protected=_protected_logs(conn))
    cap_result = cap_finished_logs(
        home, config, dry_run=dry_run, verbose=verbose, unfinished=_unfinished_logs(conn)
    )
    run_result = prune_runs(home, conn, config, dry_run=dry_run, verbose=verbose)
    audit_result = prune_audit(conn, config, dry_run=dry_run)
    rotated = rotate_logs(home, dry_run=dry_run)
    conn.close()

    mb = log_result["logs_bytes"] / 1_048_576
    print(f"\nLogs: {log_result['logs_deleted']} file(s), {mb:.1f} MB")
    for proj, d in (log_result.get("per_project") or {}).items():
        print(f"  {proj}: {d['files']} files  {d['bytes']//1024}KB  (>{d['log_days']}d)")
    cap_mb = cap_result["logs_bytes_reclaimed"] / 1_048_576
    print(f"Capped: {cap_result['logs_capped']} file(s), {cap_mb:.1f} MB reclaimed")
    print(f"Runs: {run_result['runs_deleted']} record(s)")
    print(f"Audit: {audit_result['audit_deleted']} row(s)")
    if rotated:
        print(f"Rotated: {', '.join(rotated)}")

    if dry_run:
        print("\n(Pass --apply to actually delete.)")

    # Write prune manifest
    manifest_path = home / "prune_log.jsonl"
    entry = {
        "ts": _now_iso(),
        "dry_run": dry_run,
        "logs_deleted": log_result["logs_deleted"],
        "logs_bytes": log_result["logs_bytes"],
        "logs_capped": cap_result["logs_capped"],
        "logs_bytes_reclaimed": cap_result["logs_bytes_reclaimed"],
        "runs_deleted": run_result["runs_deleted"],
    }
    with open(manifest_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")

    return 0
