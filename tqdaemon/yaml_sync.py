"""YAML queue-definition sync: mtime-keyed parse cache and per-project DB reconcile.

Extracted from daemon.py. daemon re-exports these names for existing callers.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import threading
from pathlib import Path

from tqdaemon._db_tasks import _upsert_task_no_commit, stamp_provenance
from tqdaemon._time import _now
from tqdaemon.config import Project
from tqdaemon.db import parse_task_yaml_many

# W4.3: mtime cache — skip re-parsing YAML files that haven't changed.
# Key: absolute path string; value: (mtime_ns, YAML_PARSER_GEN, task_ids, file_bytes).
# Bump YAML_PARSER_GEN when parse_task_yaml_many's field set changes so a
# deploy is not invisible until every file's mtime changes (F-077).
YAML_PARSER_GEN = 1
_yaml_mtime_cache: dict[str, tuple[int, int, tuple[str, ...] | None, int]] = {}
# O1: cap to prevent unbounded memory growth on large repos.
_YAML_CACHE_MAX = 5000
_YAML_CACHE_MAX_BYTES = 64 * 1024 * 1024
_yaml_cache_hits = 0
_yaml_cache_misses = 0
_yaml_cache_bytes = 0


def iter_queue_files(queue_dir: Path):
    """F-061: canonical queue definition enumeration (.yaml and .yml)."""
    from itertools import chain

    yield from sorted(
        chain(queue_dir.glob("*.yaml"), queue_dir.glob("*.yml")),
        key=lambda p: p.name.lower(),
    )


def clear_yaml_mtime_cache() -> None:
    """Test helper: drop process-local YAML parse cache."""
    global _yaml_cache_bytes, _yaml_cache_hits, _yaml_cache_misses
    _yaml_mtime_cache.clear()
    _yaml_cache_bytes = 0
    _yaml_cache_hits = 0
    _yaml_cache_misses = 0


def yaml_cache_stats() -> dict[str, int]:
    """Return process-local YAML parse-cache counters for ``tq doctor``."""
    return {
        "entries": len(_yaml_mtime_cache),
        "bytes": _yaml_cache_bytes,
        "max_entries": _YAML_CACHE_MAX,
        "max_bytes": _YAML_CACHE_MAX_BYTES,
        "hits": _yaml_cache_hits,
        "misses": _yaml_cache_misses,
    }


def _in_queue_dir(key: str, qdir_str: str, qdir_resolved: Path) -> bool:
    """True if cache key lives directly under the queue dir (string compare first, resolve as fallback)."""
    if os.path.dirname(key) == qdir_str:
        return True
    return Path(key).resolve().parent == qdir_resolved


# The tick and HTTP threads (/api/sync, relocate) both sync projects: serialize them so the
# module caches above and the existing-vs-scanned reconcile never interleave (audit 2026-11).
_SYNC_LOCK = threading.RLock()


def _sync_project(*args, **kwargs) -> dict:
    """Thread-safe entry point; see ``_sync_project_unlocked`` for the reconcile itself."""
    with _SYNC_LOCK:
        return _sync_project_unlocked(*args, **kwargs)


def _sync_project_unlocked(
    conn,
    project: Project,
    *,
    cache: dict | None = None,
    home: Path | None = None,
    force: bool = False,
) -> dict[str, int]:
    """Reconcile one project's YAMLs into the DB. ``force`` ignores the mtime cache (re-parse all).

    Returns ``{"upserted": n, "deleted": m}`` for this call (F-119 metrics).
    """
    global _yaml_cache_bytes, _yaml_cache_hits, _yaml_cache_misses
    empty = {"upserted": 0, "deleted": 0}
    if cache is None:
        cache = _yaml_mtime_cache
    # W6.18: warn and skip if project root is missing (was silently swallowed by queue_dir check).
    if not project.root.exists():
        print(
            f"[queue] {_ts()} warn: {project.name}: root directory missing ({project.root}) — skipping",
            file=sys.stderr,
        )
        return empty
    if not project.queue_dir.is_dir():
        return empty
    from tqdaemon.db import get_config

    cfg = get_config(conn)
    allow_remote = bool(cfg.get("notify_allow_remote"))
    # W4.11: max queued tasks per project (default 500); warn but do not delete.
    _max_queued = int(cfg.get("max_queued_tasks_per_project", 500))
    found_task_ids: set[str] = set()
    dirty = False  # W4.3: only commit if at least one upsert was needed
    upserted = 0
    deleted = 0

    # F-060: presence vs admission — load existing IDs before scanning files.
    existing_rows = conn.execute(
        "SELECT task_id, status FROM tasks WHERE project = ?",
        (project.name,),
    ).fetchall()
    existing: dict[str, str] = {r["task_id"]: r["status"] for r in existing_rows}
    admitted = sum(1 for status in existing.values() if status not in ("disabled", "completed"))

    # F-079: materialize the listing first. Path.glob on a vanished dir returns []
    # (no OSError); if we then DELETE every missing task_id we wipe the project.
    try:
        yaml_paths = list(iter_queue_files(project.queue_dir))
    except OSError as exc:
        print(
            f"[queue] {_ts()} warn: {project.name}: queue dir listing failed ({exc}) — skipping sync cleanup",
            file=sys.stderr,
        )
        return empty
    if not project.queue_dir.is_dir():
        print(
            f"[queue] {_ts()} warn: {project.name}: queue dir vanished during listing "
            f"({project.queue_dir}) — skipping sync cleanup",
            file=sys.stderr,
        )
        return empty
    enumerated_ok = True

    pending_cache: dict[str, tuple] = {}
    for yaml_path in yaml_paths:
        try:
            # W4.11: skip oversized YAML files before read_text (512 KiB cap).
            _YAML_SIZE_CAP = 512 * 1024
            try:
                yaml_size = yaml_path.stat().st_size
            except OSError:
                yaml_size = 0
            if yaml_size > _YAML_SIZE_CAP:
                print(
                    f"[queue] {_ts()} warn: {project.name}: skipping {yaml_path.name} "
                    f"({yaml_size // 1024} KiB > {_YAML_SIZE_CAP // 1024} KiB cap)",
                    file=sys.stderr,
                )
                # F-076: do not parse, but keep rows already registered from this
                # path. Otherwise cleanup treats the file as gone and deletes them.
                for _kept in conn.execute(
                    "SELECT task_id FROM tasks WHERE project = ? AND yaml_source = ?",
                    (project.name, str(yaml_path)),
                ):
                    found_task_ids.add(_kept["task_id"])
                continue

            # W4.3: skip re-parsing files that haven't changed since last sync.
            key = str(yaml_path)
            try:
                mtime_ns = yaml_path.stat().st_mtime_ns
            except OSError:
                mtime_ns = None
            _cached = cache.get(key)
            if (
                not force
                and mtime_ns is not None
                and _cached is not None
                and len(_cached) >= 3
                and _cached[0] == mtime_ns
                and _cached[1] == YAML_PARSER_GEN
                and _cached[2] is not None
            ):
                _yaml_cache_hits += 1
                # F-040: file unchanged — skip upsert to avoid constant WAL churn
                found_task_ids.update(_cached[2])
                continue
            _yaml_cache_misses += 1
            # R10: parse_task_yaml_many returns >=1 tasks (>1 for matrix: YAMLs)
            task_list = parse_task_yaml_many(yaml_path, project, allow_remote_notify=allow_remote)
            _file_skipped = False
            for task in task_list:
                # F-060: mark present before cap logic so cleanup never deletes
                # YAML-backed tasks merely because the admission cap is full.
                found_task_ids.add(task.task_id)
                genuinely_new = task.task_id not in existing
                old_status = existing.get(task.task_id)
                re_enabled = not genuinely_new and task.enabled and old_status in ("disabled", "completed")
                if _max_queued > 0 and (genuinely_new or re_enabled) and task.enabled and admitted >= _max_queued:
                    print(
                        f"[queue] {_ts()} warn: {project.name}: task {task.task_id} skipped "
                        f"(max_queued_tasks_per_project={_max_queued} reached)",
                        file=sys.stderr,
                    )
                    _file_skipped = True
                    continue
                # Batch: execute without committing; commit once after the loop.
                _upsert_task_no_commit(conn, task)
                # F-218: the file parses again — drop a stale parse-error annotation.
                conn.execute(
                    "UPDATE tasks SET block_reason = NULL "
                    "WHERE project = ? AND task_id = ? AND block_reason LIKE 'invalid_yaml:%'",
                    (project.name, task.task_id),
                )
                upserted += 1
                if genuinely_new:
                    stamp_provenance(conn, project.name, [task.task_id], "yaml", commit=False)
                dirty = True
                if (genuinely_new or re_enabled) and task.enabled:
                    admitted += 1
                    existing[task.task_id] = "queued"
            # Only remember the file once every task was upserted (and, below, the batch
            # committed); a failed/skipped upsert must be retried on the next sync.
            if mtime_ns is not None and not _file_skipped:
                # F-235: a cache hit only needs the ids — do not pin parsed QueueTask objects.
                pending_cache[key] = (mtime_ns, YAML_PARSER_GEN, tuple(t.task_id for t in task_list), int(yaml_size))
        except Exception as exc:  # noqa: BLE001 - a bad YAML file is disabled, not the whole sync
            print(
                f"[queue] {_ts()} warn: {project.name}: failed to parse {yaml_path.name}: {exc}",
                file=sys.stderr,
            )
            # I-14: surface the parse error in tq list / the UI.
            try:
                stem = yaml_path.stem
                msg = f"invalid_yaml: {exc} in {yaml_path.name}"
                # F-218: rows already registered from this file keep their status (a
                # completed task flipped to disabled would be re-queued, i.e. re-run,
                # once the file parses again); only annotate them. A sentinel row is
                # inserted only for a file that never parsed.
                cur = conn.execute(
                    "UPDATE tasks SET block_reason = ?, updated_at = ? "
                    "WHERE project = ? AND (yaml_source = ? OR task_id = ?) AND status != 'running'",
                    (msg, _now(), project.name, str(yaml_path), stem),
                )
                known = conn.execute(
                    "SELECT 1 FROM tasks WHERE project = ? AND (yaml_source = ? OR task_id = ?) LIMIT 1",
                    (project.name, str(yaml_path), stem),
                ).fetchone()
                if known is None:
                    conn.execute(
                        "INSERT INTO tasks (project, task_id, status, enabled, block_reason, "
                        "command, enqueued_at, updated_at) VALUES (?, ?, 'disabled', 0, ?, '', ?, ?)",
                        (project.name, stem, msg, _now(), _now()),
                    )
                dirty = dirty or known is None or (cur.rowcount or 0) > 0
                found_task_ids.add(stem)
                # Keep every task already registered from this file (matrix / multi-doc
                # ids differ from the stem), or delete-missing cleanup wipes them and
                # their history on a half-saved edit.
                for _kept in conn.execute(
                    "SELECT task_id FROM tasks WHERE project = ? AND yaml_source = ?",
                    (project.name, str(yaml_path)),
                ):
                    found_task_ids.add(_kept["task_id"])
            except Exception as _exc2:  # noqa: BLE001 - a failed sentinel write must roll back, whatever the cause
                # A failed DML leaves the implicit transaction open, holding the
                # write lock and starving every HTTP write (retry, lease renew).
                if not dirty:
                    conn.rollback()
                print(
                    f"[queue] {_ts()} warn: could not upsert disabled sentinel for {yaml_path.name}: {_exc2}",
                    file=sys.stderr,
                )

    # W4.11: summary if cap was hit — re-query for accurate post-loop count.
    if _max_queued > 0:
        _final_count = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE project = ? AND status NOT IN ('disabled', 'completed')",
            (project.name,),
        ).fetchone()[0]
        if _final_count >= _max_queued:
            print(
                f"[queue] {_ts()} info: {project.name}: {_final_count} tasks admitted "
                f"(cap max_queued_tasks_per_project={_max_queued}).",
                file=sys.stderr,
            )

    # W4.3: batch commit for all upserts accumulated in the parse loop above.
    if dirty:
        try:
            conn.commit()
        except sqlite3.Error as exc:
            # F-158: roll back so the connection is not left holding a failed txn
            # that blocks later HTTP writers on the same connection.
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            pending_cache.clear()
            print(f"[queue] {_ts()} warn: {project.name}: batch upsert commit failed: {exc}", file=sys.stderr)
    for _k, _v in pending_cache.items():
        old = cache.pop(_k, None)
        if cache is _yaml_mtime_cache and old is not None:
            _yaml_cache_bytes -= int(old[3]) if len(old) >= 4 else 0
        cache[_k] = _v
        if cache is _yaml_mtime_cache:
            _yaml_cache_bytes += int(_v[3])
        while len(cache) > _YAML_CACHE_MAX or (
            cache is _yaml_mtime_cache and _yaml_cache_bytes > _YAML_CACHE_MAX_BYTES
        ):
            evicted_key = next(iter(cache))
            evicted = cache.pop(evicted_key)
            if cache is _yaml_mtime_cache:
                _yaml_cache_bytes -= int(evicted[3]) if len(evicted) >= 4 else 0

    # Clean up tasks in database that no longer have YAML files.
    # A transient SQLite lock here must never crash the daemon (it did:
    # this DELETE previously had no exception handling, unlike the parse
    # loop above it -- one "database is locked" here killed the whole
    # process, orphaning every currently-running task's monitoring).
    # F-079: only DELETE when enumeration succeeded and the dir is still present.
    # Guard: if the project had tasks before and now zero YAMLs were found, the
    # queue dir may be temporarily empty (network mount, transient FS hiccup).
    # Skip cleanup in that case rather than wiping every task.
    if found_task_ids == set() and existing:
        print(
            f"[queue] {_ts()} warn: {project.name}: 0 YAML files found but "
            f"{len(existing)} task(s) in DB — skipping cleanup (transient empty dir?)",
            file=sys.stderr,
            flush=True,
        )
        # Still evict mtime cache entries for paths that are gone (F-070 partial).
        live_keys = {str(p) for p in yaml_paths}
        qdir_resolved = project.queue_dir.resolve()
        qdir_str = str(project.queue_dir)
        for k in list(cache):
            try:
                if k not in live_keys and _in_queue_dir(k, qdir_str, qdir_resolved):
                    old = cache.pop(k, None)
                    if cache is _yaml_mtime_cache and old is not None:
                        _yaml_cache_bytes -= int(old[3]) if len(old) >= 4 else 0
            except (OSError, TypeError, ValueError):
                continue
        return {"upserted": upserted, "deleted": 0}
    if enumerated_ok and project.queue_dir.is_dir():
        try:
            db_tasks = conn.execute("SELECT task_id, status FROM tasks WHERE project = ?", (project.name,)).fetchall()
            deleted_any = False
            for row in db_tasks:
                tid = row["task_id"]
                if tid not in found_task_ids:
                    if row["status"] != "running":
                        conn.execute(
                            "DELETE FROM tasks WHERE project = ? AND task_id = ?",
                            (project.name, tid),
                        )
                        conn.execute(
                            "DELETE FROM runs WHERE project = ? AND task_id = ?",
                            (project.name, tid),
                        )
                        deleted_any = True
                        deleted += 1
                        print(f"[queue] {_ts()} unregistered old task: {project.name}/{tid}", flush=True)
            if deleted_any:
                conn.commit()
        except sqlite3.Error as exc:
            print(
                f"[queue] {_ts()} warn: {project.name}: sync cleanup failed (will retry next sync): {exc}",
                file=sys.stderr,
            )

    # F-070: drop cache entries for paths no longer present under this queue_dir.
    if enumerated_ok and project.queue_dir.is_dir():
        live_keys = {str(p) for p in yaml_paths}
        qdir_resolved = project.queue_dir.resolve()
        qdir_str = str(project.queue_dir)
        stale = []
        for k in list(cache):
            try:
                if k not in live_keys and _in_queue_dir(k, qdir_str, qdir_resolved):
                    stale.append(k)
            except (OSError, TypeError, ValueError):
                continue
        for k in stale:
            old = cache.pop(k, None)
            if cache is _yaml_mtime_cache and old is not None:
                _yaml_cache_bytes -= int(old[3]) if len(old) >= 4 else 0
    return {"upserted": upserted, "deleted": deleted}


from tqdaemon._time import _ts  # noqa: E402 (after local imports)
