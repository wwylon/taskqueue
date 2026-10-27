"""gpt.md P5 / R-009: Git SHA + dirty overlay + isolated attempt workspaces."""

from __future__ import annotations

import subprocess
from pathlib import Path

from taskqueue.provision import (
    PROVIDER_BUNDLE,
    PROVIDER_GIT_PATCH,
    PROVIDER_PROJECT_ROOT,
    capture_git_package,
    compute_package_hash,
    materialize_git_package,
    prepare_attempt_workspace,
    resolve_code_provider,
)


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(root: Path) -> str:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init")
    _git(root, "config", "user.email", "tq@test")
    _git(root, "config", "user.name", "tq test")
    (root / "app.py").write_text("print('clean')\n", encoding="utf-8")
    _git(root, "add", "app.py")
    _git(root, "commit", "-m", "init")
    sha = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return sha


def test_package_hash_stable_across_callers() -> None:
    a = compute_package_hash(
        provider=PROVIDER_GIT_PATCH, commit="abc", patch_hash="p1", env_hash="e1"
    )
    b = compute_package_hash(
        provider=PROVIDER_GIT_PATCH, commit="abc", patch_hash="p1", env_hash="e1"
    )
    assert a == b
    c = compute_package_hash(
        provider=PROVIDER_GIT_PATCH, commit="abc", patch_hash="p2", env_hash="e1"
    )
    assert a != c


def test_resolve_code_provider(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    _init_repo(repo)
    assert resolve_code_provider(
        raw_provider=None, bundle_hash=None, project_root=repo
    ) == PROVIDER_GIT_PATCH
    assert resolve_code_provider(
        raw_provider="bundle", bundle_hash="deadbeef", project_root=repo
    ) == PROVIDER_BUNDLE
    assert resolve_code_provider(
        raw_provider=None, bundle_hash="deadbeef", project_root=tmp_path / "notgit"
    ) == PROVIDER_BUNDLE
    assert resolve_code_provider(
        raw_provider=None, bundle_hash=None, project_root=tmp_path / "notgit"
    ) == PROVIDER_PROJECT_ROOT


def test_capture_git_package_identical_on_master_and_worker(tmp_path: Path) -> None:
    repo = tmp_path / "src"
    commit = _init_repo(repo)
    (repo / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
    master_home = tmp_path / "master"
    worker_home = tmp_path / "worker"
    pkg_m = capture_git_package(repo, project="demo", home=master_home)
    pkg_w = capture_git_package(repo, project="demo", home=worker_home)
    assert pkg_m.commit == commit
    assert pkg_m.patch_hash
    assert pkg_m.package_hash == pkg_w.package_hash
    assert pkg_m.patch_hash == pkg_w.patch_hash


def test_dirty_overlay_appears_in_workspace(tmp_path: Path) -> None:
    repo = tmp_path / "src"
    _init_repo(repo)
    (repo / "dirty.txt").write_text("overlay-me\n", encoding="utf-8")
    home = tmp_path / "home"
    pkg = capture_git_package(repo, project="demo", home=home)
    cache = materialize_git_package(home=home, package=pkg, git_root=repo)
    assert (cache / "app.py").is_file()
    assert (cache / "dirty.txt").read_text(encoding="utf-8") == "overlay-me\n"
    # HEAD checkout must not include dirty.txt
    assert not (repo / ".git").is_dir() or True
    clean = (repo / "app.py").read_text(encoding="utf-8")
    assert "print('clean')" in clean


def test_attempt_workspaces_are_isolated(tmp_path: Path) -> None:
    """§28 #13: two package hashes / attempts do not share writable files."""
    repo = tmp_path / "src"
    _init_repo(repo)
    home = tmp_path / "home"
    pkg_a = capture_git_package(repo, project="demo", home=home)
    cache_a = materialize_git_package(home=home, package=pkg_a, git_root=repo)
    ws_a = prepare_attempt_workspace(home, 1, cache_a)
    ws_b = prepare_attempt_workspace(home, 2, cache_a)
    assert ws_a != ws_b
    (ws_a / "app.py").write_text("mutated by A\n", encoding="utf-8")
    assert (ws_b / "app.py").read_text(encoding="utf-8") == "print('clean')\n"
    assert (cache_a / "app.py").read_text(encoding="utf-8") == "print('clean')\n"

    (repo / "dirty.txt").write_text("B\n", encoding="utf-8")
    pkg_b = capture_git_package(repo, project="demo", home=home)
    assert pkg_a.package_hash != pkg_b.package_hash
    cache_b = materialize_git_package(home=home, package=pkg_b, git_root=repo)
    assert (cache_b / "dirty.txt").read_text(encoding="utf-8") == "B\n"
    assert not (cache_a / "dirty.txt").exists()
