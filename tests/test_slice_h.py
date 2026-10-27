"""Slice H — auth, safe tar, export/import."""

from __future__ import annotations

import io
import os
import tarfile
from pathlib import Path

import pytest

from taskqueue.auth import check_token, extract_request_token
from taskqueue.export_import import export_home, import_home
from taskqueue.safetar import assert_safe_member, safe_extract


def test_auth_optional_when_empty() -> None:
    assert check_token({"auth_token": ""}, None) is True
    assert check_token({"auth_token": "secret"}, None) is False
    assert check_token({"auth_token": "secret"}, "secret") is True
    assert check_token({"auth_token": "secret"}, "wrong") is False


def test_extract_bearer_and_header() -> None:
    assert extract_request_token({"Authorization": "Bearer abc"}) == "abc"
    assert extract_request_token({"X-TQ-Token": "xyz"}) == "xyz"
    assert extract_request_token({}, "q") == "q"


def test_safe_extract_rejects_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "bad.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo(name="../escape.txt")
        data = b"nope"
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    dest = tmp_path / "out"
    dest.mkdir()
    with tarfile.open(archive, "r:gz") as tar:
        with pytest.raises(ValueError, match="unsafe"):
            safe_extract(tar, dest)


def test_safe_member_allows_normal(tmp_path: Path) -> None:
    info = tarfile.TarInfo(name="ok/file.txt")
    assert_safe_member(tmp_path, info)


def test_export_import_roundtrip(tmp_path: Path) -> None:
    from taskqueue.db import open_db

    home = tmp_path / "home"
    home.mkdir()
    open_db(home / "queue.db").close()
    (home / "projects.yaml").write_text("projects: {}\n", encoding="utf-8")
    archive = tmp_path / "exp.tar.gz"
    export_home(home, archive)
    assert archive.is_file()

    home2 = tmp_path / "home2"
    info = import_home(home2, archive)
    assert Path(info["db"]).is_file()
    assert (home2 / "projects.yaml").is_file()

    with pytest.raises(FileExistsError):
        import_home(home2, archive, force=False)
    import_home(home2, archive, force=True)


def test_import_clears_stale_wal_at_destination(tmp_path: Path) -> None:
    """Regression for bug #3: a prior uncleanly-shut-down daemon can leave
    -wal/-shm sidecars at the import destination. SQLite matches those to a
    DB by filename, not content -- without clearing them, the next open()
    of the freshly-imported queue.db would replay stale WAL frames against
    it, corrupting or reverting the import."""
    from datetime import UTC, datetime
    from taskqueue.db import open_db, get_task

    # Source home: has a real task in it, exported.
    src_home = tmp_path / "src"
    src_home.mkdir()
    src_db = open_db(src_home / "queue.db")
    now = datetime.now(UTC).isoformat()
    src_db.execute(
        "INSERT INTO tasks (project, task_id, status, command, enqueued_at, updated_at, enabled) "
        "VALUES ('p', 't1', 'queued', 'true', ?, ?, 1)",
        (now, now),
    )
    src_db.commit()
    src_db.close()
    archive = tmp_path / "exp.tar.gz"
    export_home(src_home, archive)

    # Destination home: pre-seed a stale -wal/-shm from an unrelated prior
    # DB (simulating a daemon that crashed instead of shutting down clean).
    dest_home = tmp_path / "dest"
    dest_home.mkdir()
    (dest_home / "queue.db-wal").write_bytes(b"\x00" * 64)
    (dest_home / "queue.db-shm").write_bytes(b"\x00" * 32)

    import_home(dest_home, archive, force=True)

    assert not (dest_home / "queue.db-wal").exists()
    assert not (dest_home / "queue.db-shm").exists()

    # Imported DB opens clean and has the source's task, not corrupted by
    # replaying the stale WAL frames.
    dest_db = open_db(dest_home / "queue.db")
    row = get_task(dest_db, "p", "t1")
    assert row is not None
    dest_db.close()


def test_export_refuses_running_daemon(tmp_path: Path) -> None:
    from taskqueue.db import open_db

    home = tmp_path / "home"
    home.mkdir()
    open_db(home / "queue.db").close()
    (home / "daemon.pid").write_text(str(os.getpid()), encoding="utf-8")
    with pytest.raises(RuntimeError, match="daemon is running"):
        export_home(home, tmp_path / "x.tar.gz")


def test_bind_host_and_auth_config_defaults(tmp_path: Path) -> None:
    from taskqueue.config import validate_config
    from taskqueue.db import get_config, open_db

    assert validate_config("bind_host", "127.0.0.1") is None
    assert validate_config("auth_token", "x" * 10) is None
    assert validate_config("prevent_sleep", "1") is None
    assert validate_config("bind_host", "") is not None
    assert validate_config("limit_decrease_policy", "ignore") is None
    assert validate_config("limit_decrease_policy", "evict") is None
    assert validate_config("limit_decrease_policy", "nope") is not None

    cfg = get_config(open_db(tmp_path / "q.db"))
    assert cfg.get("bind_host") == "127.0.0.1"
    assert cfg.get("auth_token") == ""
    assert cfg.get("prevent_sleep") == 1
