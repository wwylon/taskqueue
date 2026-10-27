"""Code packaging: Git SHA + dirty overlay + per-attempt workspaces (gpt.md R-009 / P5).

Providers (preference order when auto-selecting):
  1. git_patch — base commit + content-addressed dirty/untracked overlay
  2. bundle — existing content-hashed tarball (kept; never applied onto shared project.root)
  3. project_root — run from the configured checkout (legacy / local default)
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path


PROVIDER_GIT_PATCH = "git_patch"
PROVIDER_BUNDLE = "bundle"
PROVIDER_PROJECT_ROOT = "project_root"

VALID_CODE_PROVIDERS = frozenset({
    PROVIDER_GIT_PATCH,
    PROVIDER_BUNDLE,
    PROVIDER_PROJECT_ROOT,
})


@dataclass(frozen=True)
class CodePackage:
    provider: str
    package_hash: str
    commit: str = ""
    patch_hash: str = ""
    env_hash: str = ""
    bundle_hash: str = ""
    project: str = ""

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v != "" and v is not None}


def code_package_from_dict(data: dict | None) -> CodePackage | None:
    if not isinstance(data, dict) or not data.get("package_hash") or not data.get("provider"):
        return None
    return CodePackage(
        provider=str(data["provider"]),
        package_hash=str(data["package_hash"]),
        commit=str(data.get("commit") or ""),
        patch_hash=str(data.get("patch_hash") or ""),
        env_hash=str(data.get("env_hash") or ""),
        bundle_hash=str(data.get("bundle_hash") or ""),
        project=str(data.get("project") or ""),
    )


def packages_dir(home: Path, project: str) -> Path:
    return home / "packages" / project


def code_cache_dir(home: Path, package_hash: str) -> Path:
    return home / "cache" / "code" / package_hash


def attempt_workspace_dir(home: Path, run_id: int) -> Path:
    return home / "runs" / str(int(run_id)) / "work"


def overlay_path(home: Path, project: str, patch_hash: str) -> Path:
    return packages_dir(home, project) / f"{patch_hash}.tar.gz"


def compute_package_hash(
    *,
    commit: str = "",
    patch_hash: str = "",
    env_hash: str = "",
    bundle_hash: str = "",
    provider: str = PROVIDER_GIT_PATCH,
) -> str:
    """Stable ExecutionPackageID (gpt.md §15). Same inputs → same hash on master and worker."""
    payload = "\0".join([
        provider,
        commit or "",
        patch_hash or "",
        env_hash or "",
        bundle_hash or "",
    ]).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def env_lock_hash(project_root: Path) -> str:
    lock = project_root / "uv.lock"
    if not lock.is_file():
        lock = project_root / "poetry.lock"
    if not lock.is_file():
        return ""
    h = hashlib.sha256()
    with open(lock, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def _run_git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=check,
        encoding="utf-8",
        errors="replace",
    )


def is_git_repo(root: Path) -> bool:
    r = _run_git(root, "rev-parse", "--is-inside-work-tree", check=False)
    return r.returncode == 0 and (r.stdout or "").strip() == "true"


def git_head_commit(root: Path) -> str | None:
    r = _run_git(root, "rev-parse", "HEAD", check=False)
    if r.returncode != 0:
        return None
    sha = (r.stdout or "").strip()
    return sha or None


def _dirty_paths(root: Path) -> list[str]:
    """Relative paths (posix) that differ from HEAD: modified, deleted, untracked."""
    r = _run_git(root, "status", "--porcelain", "-uall", check=False)
    if r.returncode != 0:
        return []
    paths: list[str] = []
    for line in (r.stdout or "").splitlines():
        if len(line) < 4:
            continue
        # XY PATH or XY ORIG -> PATH
        rest = line[3:]
        if " -> " in rest:
            rest = rest.split(" -> ", 1)[1]
        rel = rest.strip().strip('"').replace("\\", "/")
        if rel:
            paths.append(rel)
    # unique, stable order
    return sorted(set(paths))


def overlay_content_hash(root: Path, paths: list[str]) -> str:
    """Canonical hash of dirty file contents (independent of gzip timestamps)."""
    h = hashlib.sha256()
    for rel in paths:
        posix = rel.replace("\\", "/")
        h.update(posix.encode("utf-8"))
        h.update(b"\0")
        abs_p = root / rel
        if abs_p.is_file():
            h.update(abs_p.read_bytes())
        else:
            h.update(b"deleted")
        h.update(b"\n")
    return h.hexdigest()


def build_overlay_archive(root: Path, dest: Path, paths: list[str]) -> None:
    """Write a gzip tar of dirty/untracked files (mtime-stable)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(dest, "w:gz", compresslevel=6) as tar:
        for rel in paths:
            abs_p = root / rel
            if not abs_p.is_file():
                continue
            info = tar.gettarinfo(str(abs_p), arcname=rel.replace("\\", "/"))
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            with open(abs_p, "rb") as f:
                tar.addfile(info, f)


def capture_git_package(project_root: Path, *, project: str, home: Path) -> CodePackage:
    """Capture HEAD + dirty overlay under ``home/packages/<project>/``."""
    if not is_git_repo(project_root):
        raise ValueError(f"not a git repo: {project_root}")
    commit = git_head_commit(project_root)
    if not commit:
        raise ValueError(f"cannot resolve HEAD in {project_root}")
    dirty = _dirty_paths(project_root)
    env_h = env_lock_hash(project_root)
    if not dirty:
        patch_hash = ""
    else:
        packages_dir(home, project).mkdir(parents=True, exist_ok=True)
        patch_hash = overlay_content_hash(project_root, dirty)
        final = overlay_path(home, project, patch_hash)
        if not final.is_file():
            tmp = packages_dir(home, project) / f".overlay-{os.getpid()}.tar.gz"
            try:
                build_overlay_archive(project_root, tmp, dirty)
                shutil.move(str(tmp), str(final))
            finally:
                if tmp.exists():
                    tmp.unlink(missing_ok=True)
        meta = {
            "commit": commit,
            "paths": dirty,
            "patch_hash": patch_hash,
        }
        (packages_dir(home, project) / f"{patch_hash}.json").write_text(
            json.dumps(meta, indent=2), encoding="utf-8"
        )
    pkg_hash = compute_package_hash(
        provider=PROVIDER_GIT_PATCH,
        commit=commit,
        patch_hash=patch_hash,
        env_hash=env_h,
    )
    return CodePackage(
        provider=PROVIDER_GIT_PATCH,
        package_hash=pkg_hash,
        commit=commit,
        patch_hash=patch_hash,
        env_hash=env_h,
        project=project,
    )


def package_from_bundle(project: str, bundle_hash: str, *, env_hash: str = "") -> CodePackage:
    pkg_hash = compute_package_hash(
        provider=PROVIDER_BUNDLE,
        bundle_hash=bundle_hash,
        env_hash=env_hash,
    )
    return CodePackage(
        provider=PROVIDER_BUNDLE,
        package_hash=pkg_hash,
        bundle_hash=bundle_hash,
        env_hash=env_hash,
        project=project,
    )


def apply_overlay_tar(workspace: Path, overlay: Path) -> None:
    from taskqueue.safetar import safe_extract

    with tarfile.open(overlay, "r:gz") as tar:
        safe_extract(tar, workspace)


def materialize_git_package(
    *,
    home: Path,
    package: CodePackage,
    git_root: Path,
    fetch_overlay: Path | None = None,
) -> Path:
    """Build immutable code cache for a git_patch package. Returns cache path."""
    cache = code_cache_dir(home, package.package_hash)
    marker = cache / ".tq_package.json"
    if cache.is_dir() and marker.is_file():
        return cache
    if cache.exists():
        shutil.rmtree(cache, ignore_errors=True)
    cache.mkdir(parents=True, exist_ok=True)

    # Prefer git archive from the local clone (worker must already have the commit).
    r = _run_git(git_root, "cat-file", "-e", f"{package.commit}^{{commit}}", check=False)
    if r.returncode != 0:
        shutil.rmtree(cache, ignore_errors=True)
        raise FileNotFoundError(
            f"commit {package.commit} not present in {git_root}; "
            "sync/pull the project on the worker first"
        )

    with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        arch = subprocess.run(
            ["git", "-C", str(git_root), "archive", "--format=tar", package.commit],
            capture_output=True,
            check=True,
        )
        tmp_path.write_bytes(arch.stdout)
        with tarfile.open(tmp_path, "r:") as tar:
            tar.extractall(cache, filter="data")
    finally:
        tmp_path.unlink(missing_ok=True)

    if package.patch_hash:
        overlay = fetch_overlay or overlay_path(home, package.project, package.patch_hash)
        if not overlay.is_file():
            shutil.rmtree(cache, ignore_errors=True)
            raise FileNotFoundError(f"missing overlay {overlay}")
        apply_overlay_tar(cache, overlay)

    marker.write_text(json.dumps(package.to_dict(), indent=2), encoding="utf-8")
    return cache


def materialize_bundle_package(
    home: Path,
    package: CodePackage,
    *,
    extracted_bundle_dir: Path,
) -> Path:
    """Copy an already-extracted bundle into the content-addressed code cache."""
    cache = code_cache_dir(home, package.package_hash)
    marker = cache / ".tq_package.json"
    if cache.is_dir() and marker.is_file():
        return cache
    if cache.exists():
        shutil.rmtree(cache, ignore_errors=True)
    shutil.copytree(extracted_bundle_dir, cache)
    marker.write_text(json.dumps(package.to_dict(), indent=2), encoding="utf-8")
    return cache


def prepare_attempt_workspace(home: Path, run_id: int, code_cache: Path) -> Path:
    """Per-attempt writable tree cloned from the immutable code cache (test #13)."""
    work = attempt_workspace_dir(home, run_id)
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(code_cache, work, ignore=shutil.ignore_patterns(".tq_package.json"))
    return work


def resolve_code_provider(
    *,
    raw_provider: str | None,
    bundle_hash: str | None,
    project_root: Path | None,
) -> str:
    """Pick provider: explicit YAML wins; else git_patch if repo; else bundle; else project_root."""
    if raw_provider:
        p = str(raw_provider).strip().lower()
        if p not in VALID_CODE_PROVIDERS:
            raise ValueError(
                f"code.provider must be one of {sorted(VALID_CODE_PROVIDERS)}, got {raw_provider!r}"
            )
        return p
    if project_root is not None and is_git_repo(project_root):
        return PROVIDER_GIT_PATCH
    if bundle_hash:
        return PROVIDER_BUNDLE
    return PROVIDER_PROJECT_ROOT


def gc_orphan_packages(home: Path, db_path: Path) -> int:
    """F-100: Remove overlay tarballs in home/packages/ with no matching active run.

    Returns the number of files deleted.  Safe to call without holding any lock;
    only removes files whose patch_hash is absent from all non-terminal run rows.
    """
    import sqlite3 as _sqlite3

    packages_root = home / "packages"
    if not packages_root.is_dir():
        return 0

    # Collect patch_hash values referenced by any non-terminal run (running/queued/suspended).
    # We check package_hash column — overlay files are named <patch_hash>.tar.gz inside
    # home/packages/<project>/.  We join on code_package JSON to get the actual patch_hash.
    try:
        conn = _sqlite3.connect(str(db_path), timeout=5.0)
        conn.row_factory = _sqlite3.Row
        rows = conn.execute(
            "SELECT code_package FROM runs WHERE status IN ('running','queued','suspended')"
            " AND code_package IS NOT NULL"
        ).fetchall()
        conn.close()
    except Exception:
        return 0

    live_patch_hashes: set[str] = set()
    for r in rows:
        try:
            pkg = json.loads(r["code_package"] or "{}")
            ph = pkg.get("patch_hash") or ""
            if ph:
                live_patch_hashes.add(ph)
        except Exception:
            pass

    import time as _time
    _now = _time.time()
    _min_age_sec = 300.0  # E7: skip packages newer than 5 min (may be mid-capture)

    deleted = 0
    for proj_dir in packages_root.iterdir():
        if not proj_dir.is_dir():
            continue
        for f in list(proj_dir.iterdir()):
            if f.suffix not in (".gz", ".json") or f.stem.startswith("."):
                continue
            # stem is the patch_hash (e.g. "abc123.tar" → stem "abc123.tar" for .gz)
            patch_hash = f.stem.removesuffix(".tar") if f.suffix == ".gz" else f.stem
            if patch_hash not in live_patch_hashes:
                try:
                    if _now - f.stat().st_mtime < _min_age_sec:
                        continue  # too young — may be a concurrent capture in progress
                    f.unlink(missing_ok=True)
                    deleted += 1
                except OSError:
                    pass
    return deleted
