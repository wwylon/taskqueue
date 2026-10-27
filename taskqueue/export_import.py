"""Manual master home export / import (move between hosts)."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from taskqueue.safetar import safe_extract


MANIFEST_NAME = "tq_export_manifest.json"


def _daemon_pid_alive(home: Path) -> bool:
    # daemon.pid is "PID:create_time"; the shared parser also rejects a reused PID.
    from taskqueue._cli_helpers import _daemon_running

    return _daemon_running(home) is not None


def _backup_db(src: Path, dest: Path) -> None:
    """Consistent SQLite snapshot (WAL-safe)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    src_conn = sqlite3.connect(str(src))
    try:
        dst_conn = sqlite3.connect(str(dest))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


def export_home(
    home: Path,
    dest: Path,
    *,
    with_logs: bool = False,
    allow_running_daemon: bool = False,
) -> Path:
    """Pack projects.yaml + queue.db (+ optional logs/) into dest .tar.gz."""
    import io

    home = home.resolve()
    dest = Path(dest)
    if not str(dest).endswith(".tar.gz"):
        dest = Path(str(dest) + ".tar.gz")
    dest.parent.mkdir(parents=True, exist_ok=True)

    if not allow_running_daemon and _daemon_pid_alive(home):
        raise RuntimeError(
            "daemon is running; stop it before export (or pass allow_running_daemon)"
        )

    db = home / "queue.db"
    registry = home / "projects.yaml"
    if not db.is_file():
        raise FileNotFoundError(f"missing {db}")

    files = ["queue.db"]
    if registry.is_file():
        files.append("projects.yaml")
    logs = home / "logs"
    if with_logs and logs.is_dir():
        files.append("logs/")
    manifest: dict[str, Any] = {
        "format": 1,
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "with_logs": with_logs,
        "files": files,
    }

    with tempfile.TemporaryDirectory(prefix="tq-export-") as tmp:
        tmp_path = Path(tmp)
        snap = tmp_path / "queue.db"
        _backup_db(db, snap)
        with tarfile.open(dest, "w:gz") as tar:
            data = json.dumps(manifest, indent=2).encode("utf-8")
            info = tarfile.TarInfo(MANIFEST_NAME)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
            tar.add(snap, arcname="queue.db")
            if registry.is_file():
                tar.add(registry, arcname="projects.yaml")
            if with_logs and logs.is_dir():
                tar.add(logs, arcname="logs")
    return dest


def import_home(
    home: Path,
    archive: Path,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Extract export archive into home. Refuses if queue.db exists unless force."""
    home = Path(home)
    archive = Path(archive)
    if not archive.is_file():
        raise FileNotFoundError(archive)
    home.mkdir(parents=True, exist_ok=True)
    # F-025: always refuse if daemon is alive — os.replace() while the daemon
    # holds the file open leaves existing connections on the old inode and
    # corrupts the new DB with stale WAL frames. --force does not bypass this.
    if _daemon_pid_alive(home):
        raise RuntimeError(
            "daemon is running — stop it first (tq stop) before importing"
        )
    db = home / "queue.db"
    if db.is_file() and not force:
        raise FileExistsError(
            f"{db} exists; stop the daemon and re-run with --force to overwrite"
        )

    with tempfile.TemporaryDirectory(prefix="tq-import-") as tmp:
        tmp_path = Path(tmp)
        with tarfile.open(archive, "r:gz") as tar:
            safe_extract(tar, tmp_path)
        man_path = tmp_path / MANIFEST_NAME
        manifest: dict[str, Any] = {}
        if man_path.is_file():
            manifest = json.loads(man_path.read_text(encoding="utf-8"))
        src_db = tmp_path / "queue.db"
        if not src_db.is_file():
            raise ValueError("archive missing queue.db")
        tmp_db = home / "queue.db.importing"
        if tmp_db.exists():
            tmp_db.unlink()
        shutil.copy2(src_db, tmp_db)
        # A prior daemon that crashed instead of shutting down cleanly can
        # leave -wal/-shm sidecars at the destination. SQLite matches those
        # to a DB by filename, not content: on next open it would replay the
        # *old* WAL frames against the *newly imported* queue.db, silently
        # corrupting or reverting the import. Clear them before the swap.
        if db.is_file():
            # --force replaces a live history: keep a consistent copy to roll back to.
            import sqlite3
            bak = home / "queue.db.pre-import.bak"
            src_c = sqlite3.connect(str(db))
            try:
                dst_c = sqlite3.connect(str(bak))
                try:
                    src_c.backup(dst_c)
                finally:
                    dst_c.close()
            finally:
                src_c.close()
        for suffix in ("-wal", "-shm"):
            stale = home / f"queue.db{suffix}"
            if stale.exists():
                stale.unlink()
        os.replace(tmp_db, db)
        src_reg = tmp_path / "projects.yaml"
        if src_reg.is_file():
            shutil.copy2(src_reg, home / "projects.yaml")
        src_logs = tmp_path / "logs"
        if src_logs.is_dir():
            dest_logs = home / "logs"
            if dest_logs.exists():
                shutil.rmtree(dest_logs)
            shutil.copytree(src_logs, dest_logs)
        return {"home": str(home), "manifest": manifest, "db": str(db)}
