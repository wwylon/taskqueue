"""Content-addressed storage and manifest utilities for the data: YAML block."""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def build_manifest(
    glob_pattern: str,
    project_root: Path,
    *,
    max_bytes: int | None = None,
) -> list[dict[str, Any]]:
    """Build a content-addressed manifest for files matching glob_pattern.

    Returns list of {path (POSIX-relative), size, sha256}.
    """
    root = project_root.resolve()
    matched = sorted(root.glob(glob_pattern)) if "**" in glob_pattern or "*" in glob_pattern else []
    if not matched:
        # Try as literal path
        p = root / glob_pattern
        if p.is_file():
            matched = [p]
        elif p.is_dir():
            matched = sorted(p.rglob("*"))

    files = []
    total = 0
    for fp in matched:
        if not fp.is_file():
            continue
        size = fp.stat().st_size
        if max_bytes is not None and total + size > max_bytes:
            raise ValueError(
                f"data.inputs exceeds max_bytes ({max_bytes}): "
                f"total so far {total + size}"
            )
        total += size
        rel = fp.relative_to(root)
        files.append({
            "path": rel.as_posix(),
            "size": size,
            "sha256": _sha256_file(fp),
        })
    return files


def diff_manifest(
    have: list[dict[str, Any]],
    want: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return entries from `want` whose sha256 is not in `have`."""
    have_hashes = {e["sha256"] for e in have}
    return [e for e in want if e["sha256"] not in have_hashes]


def cas_blob_path(cas_root: Path, sha256: str) -> Path:
    """Return the CAS storage path for a given sha256 hex digest."""
    return cas_root / sha256[:2] / sha256


def store_blob(cas_root: Path, source: Path) -> str:
    """Hash `source` and hard-link or copy it into the CAS. Returns sha256."""
    digest = _sha256_file(source)
    dest = cas_blob_path(cas_root, digest)
    if dest.is_file():
        return digest  # already present
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    try:
        shutil.copy2(source, tmp)
        tmp.replace(dest)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return digest


def retrieve_blob(cas_root: Path, sha256: str, dest: Path) -> None:
    """Copy a CAS blob to `dest`."""
    src = cas_blob_path(cas_root, sha256)
    if not src.is_file():
        raise FileNotFoundError(f"CAS blob not found: {sha256}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    shutil.copy2(src, tmp)
    tmp.replace(dest)


def sqlite_snapshot(db_path: Path, dest: Path) -> str:
    """Create a read-consistent snapshot of a SQLite DB using VACUUM INTO.

    Returns sha256 of the snapshot.
    """
    import sqlite3
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp.db")
    try:
        conn = sqlite3.connect(str(db_path), timeout=10)
        conn.execute(f"VACUUM INTO '{tmp}'")
        conn.close()
        tmp.replace(dest)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return _sha256_file(dest)
