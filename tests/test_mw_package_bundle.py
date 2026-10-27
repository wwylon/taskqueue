"""GET /api/package and /api/bundle — code artifact serve."""

from __future__ import annotations

from pathlib import Path

from taskqueue.config import Project
from taskqueue.provision import overlay_path, packages_dir
from tests.helpers.master_worker import http_request, make_handler_env


def test_package_overlay_served(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TASKQUEUE_HOME", str(tmp_path))
    patch_hash = "abc123def456"
    overlay = overlay_path(tmp_path, "proj", patch_hash)
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_bytes(b"fake-overlay-gzip")
    root = tmp_path / "projroot"
    root.mkdir()
    projects = {"proj": Project(name="proj", root=root, queue_dir=root / "q")}
    HandlerClass, db, _ = make_handler_env(tmp_path, projects=projects)
    code, data = http_request(HandlerClass, "GET", f"/api/package/proj/{patch_hash}")
    assert code == 200
    assert data == b"fake-overlay-gzip"
    db.close()


def test_package_overlay_404(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TASKQUEUE_HOME", str(tmp_path))
    root = tmp_path / "projroot"
    root.mkdir()
    projects = {"proj": Project(name="proj", root=root, queue_dir=root / "q")}
    HandlerClass, db, _ = make_handler_env(tmp_path, projects=projects)
    code, _ = http_request(HandlerClass, "GET", "/api/package/proj/deadbeef")
    assert code == 404
    db.close()


def test_package_invalid_hash_rejected(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TASKQUEUE_HOME", str(tmp_path))
    HandlerClass, db, _ = make_handler_env(tmp_path)
    code, _ = http_request(HandlerClass, "GET", "/api/package/proj/not-hex!")
    assert code == 400
    db.close()


def test_bundle_served(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TASKQUEUE_HOME", str(tmp_path))
    bundle_hash = "a" * 64
    bundle_dir = tmp_path / "bundles" / "proj"
    bundle_dir.mkdir(parents=True)
    (bundle_dir / f"{bundle_hash}.tar.gz").write_bytes(b"bundle-bytes")
    root = tmp_path / "projroot"
    root.mkdir()
    projects = {"proj": Project(name="proj", root=root, queue_dir=root / "q")}
    HandlerClass, db, _ = make_handler_env(tmp_path, projects=projects)
    code, data = http_request(HandlerClass, "GET", f"/api/bundle/proj/{bundle_hash}")
    assert code == 200
    assert data == b"bundle-bytes"
    db.close()
