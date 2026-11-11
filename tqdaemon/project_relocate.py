"""F-240: rewrite project root + yaml_source after a folder move."""

from __future__ import annotations

import re
import shutil
import sqlite3
import time
from pathlib import Path

from tqdaemon.config import Project, load_projects, registry_path


def set_project_root(home: Path, name: str, new_root: Path) -> Path:
    """Rewrite ``root:`` for ``name`` in projects.yaml. Returns the previous root.

    Surgical line edit (preserves comments / other keys). Raises ``KeyError`` if
    the project is missing, ``ValueError`` if the file is malformed.
    """
    path = registry_path(home)
    if not path.is_file():
        raise KeyError(f"projects.yaml missing; cannot relocate {name}")
    text = path.read_text(encoding="utf-8")
    # Prefer root as the first key under the project; else any ``root:`` in the block.
    patterns = (
        re.compile(rf"^(  {re.escape(name)}:\s*\n\s+root:\s*)(.+)$", re.MULTILINE),
        re.compile(
            rf"(^  {re.escape(name)}:\s*\n(?:    .+\n)*?    root:\s*)(.+)$",
            re.MULTILINE,
        ),
    )
    m = None
    used = None
    for pat in patterns:
        m = pat.search(text)
        if m is not None:
            used = pat
            break
    if m is None or used is None:
        raise KeyError(f"project {name!r} not found in projects.yaml (or has no root:)")
    old_root = Path(m.group(2).strip().strip("\"'"))
    # Lambda (not a backslash repl string): Windows paths like ``D:\Users`` would
    # otherwise be parsed as re escapes (``\U``).
    repl = m.group(1) + str(new_root)
    path.write_text(used.sub(lambda _m: repl, text, count=1), encoding="utf-8")
    return old_root


def backup_queue_db(home: Path) -> Path:
    """Copy ``queue.db`` to ``queue.db.bak-<utc>`` next to it. Returns the backup path."""
    src = home / "queue.db"
    if not src.is_file():
        raise FileNotFoundError(f"no queue.db under {home}")
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    dest = home / f"queue.db.bak-{stamp}"
    shutil.copy2(src, dest)
    return dest


def _prefix_variants(root: Path) -> list[str]:
    raw = str(root)
    out: list[str] = []
    for s in (raw, raw.replace("\\", "/"), raw.replace("/", "\\")):
        if s and s not in out:
            out.append(s)
    try:
        resolved = str(root.resolve())
        for s in (resolved, resolved.replace("\\", "/"), resolved.replace("/", "\\")):
            if s and s not in out:
                out.append(s)
    except OSError:
        pass
    # Longest first so a nested prefix is not partially replaced.
    out.sort(key=len, reverse=True)
    return out


def rewrite_yaml_source_prefix(conn: sqlite3.Connection, old_root: Path, new_root: Path) -> int:
    """Rewrite ``tasks.yaml_source`` paths that live under ``old_root``. Returns rows updated."""
    new_s = str(new_root)
    new_slash = new_s.replace("\\", "/")
    new_back = new_s.replace("/", "\\")
    updated = 0
    rows = conn.execute(
        "SELECT project, task_id, yaml_source FROM tasks WHERE yaml_source != ''"
    ).fetchall()
    for row in rows:
        src = row["yaml_source"]
        if not src:
            continue
        new_src = None
        for old in _prefix_variants(old_root):
            if src == old or src.startswith(old + "/") or src.startswith(old + "\\"):
                rest = src[len(old) :]
                if "/" in src and "\\" not in src:
                    base = new_slash
                elif "\\" in src:
                    base = new_back
                else:
                    base = new_s
                new_src = base + rest
                break
        if new_src is None or new_src == src:
            continue
        conn.execute(
            "UPDATE tasks SET yaml_source = ? WHERE project = ? AND task_id = ?",
            (new_src, row["project"], row["task_id"]),
        )
        updated += 1
    if updated:
        conn.commit()
    return updated


def status_histogram(conn: sqlite3.Connection, project: str) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM tasks WHERE project = ? GROUP BY status",
        (project,),
    ).fetchall()
    return {str(r["status"]): int(r["n"]) for r in rows}


def count_running(conn: sqlite3.Connection, project: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM tasks WHERE project = ? AND status = 'running'",
        (project,),
    ).fetchone()
    return int(row["n"]) if row is not None else 0


def validate_new_root(new_root: Path, queue_rel: str = "experiments/operator_queue") -> str | None:
    """Return an error message if ``new_root`` is not usable, else None."""
    if not new_root.exists():
        return f"new_root does not exist: {new_root}"
    if not new_root.is_dir():
        return f"new_root is not a directory: {new_root}"
    q = Path(queue_rel) if Path(queue_rel).is_absolute() else new_root / queue_rel
    if not q.is_dir():
        return f"queue dir missing at new_root: {q}"
    return None


def load_project_or_raise(home: Path, name: str) -> Project:
    projects = load_projects(home)
    if name not in projects:
        raise KeyError(f"project {name!r} not in projects.yaml")
    return projects[name]


def relocate_project_files(
    home: Path,
    conn: sqlite3.Connection,
    project: str,
    new_root: Path,
    *,
    queue_rel: str | None = None,
) -> dict:
    """Offline/core relocate: backup DB, rewrite root + yaml_source, return summary.

    Does not touch watches or run sync. Caller must refuse while running.
    """
    proj = load_project_or_raise(home, project)
    rel = queue_rel
    if rel is None:
        # Prefer the relative queue_dir from the old project when possible.
        try:
            rel = str(proj.queue_dir.relative_to(proj.root))
        except ValueError:
            rel = "experiments/operator_queue"
    err = validate_new_root(new_root, rel)
    if err:
        raise ValueError(err)
    before = status_histogram(conn, project)
    before_n = sum(before.values())
    bak = backup_queue_db(home)
    old_root = set_project_root(home, project, new_root)
    n_src = rewrite_yaml_source_prefix(conn, old_root, new_root)
    after_projects = load_projects(home)
    new_proj = after_projects[project]
    return {
        "ok": True,
        "project": project,
        "old_root": str(old_root),
        "new_root": str(new_proj.root),
        "queue_dir": str(new_proj.queue_dir),
        "backup": str(bak),
        "yaml_source_rewritten": n_src,
        "before": {"count": before_n, "status": before},
        "project_obj": new_proj,
    }
