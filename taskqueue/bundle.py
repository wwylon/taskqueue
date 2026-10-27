"""Create project binary bundles for remote workers."""

from __future__ import annotations

import fnmatch
import gzip
import hashlib
import os
import shutil
import tarfile
import tempfile
from pathlib import Path

from taskqueue.config import Project


def create_project_bundle(
    home: Path,
    project: Project,
    *,
    project_name: str | None = None,
) -> tuple[str, Path, int]:
    """Pack project files per .tqbundle (or defaults).

    Returns (bundle_hash, dest_path, files_added).
    Raises ValueError if no files match.
    """
    name = project_name or project.name
    root = project.root
    manifest_file = root / ".tqbundle"

    includes: list[str] = []
    excludes: list[str] = []

    if manifest_file.is_file():
        for line in manifest_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("include:"):
                includes.append(line[len("include:"):].strip())
            elif line.startswith("exclude:"):
                excludes.append(line[len("exclude:"):].strip())
    else:
        includes = ["bin/**", "scripts/**", "*.py", "pyproject.toml", "uv.lock"]
        excludes = ["data/**", "build/**", "*.log", ".git/**", ".venv/**"]

    def should_include(rel_path: str) -> bool:
        rel_path = rel_path.replace("\\", "/")
        for pattern in excludes:
            if fnmatch.fnmatch(rel_path, pattern) or any(
                fnmatch.fnmatch(p, pattern) for p in rel_path.split("/")
            ):
                return False
        for pattern in includes:
            if fnmatch.fnmatch(rel_path, pattern) or any(
                fnmatch.fnmatch(p, pattern) for p in rel_path.split("/")
            ):
                return True
        return False

    temp_tar = tempfile.NamedTemporaryFile(delete=False, suffix=".tar.gz")
    temp_tar.close()

    files_added = 0
    try:
        # Reproducible archive: fixed gzip mtime and sorted walk, so unchanged files
        # always hash the same and workers do not re-download identical bundles.
        with open(temp_tar.name, "wb") as raw, gzip.GzipFile(
            filename="", fileobj=raw, mode="wb", mtime=0
        ) as gz, tarfile.open(fileobj=gz, mode="w") as tar:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames.sort()
                for f in sorted(filenames):
                    abs_p = Path(dirpath) / f
                    rel_str = str(abs_p.relative_to(root))
                    if should_include(rel_str):
                        tar.add(abs_p, arcname=rel_str)
                        files_added += 1

        if files_added == 0:
            raise ValueError("No files matched the include patterns.")

        sha256 = hashlib.sha256()
        with open(temp_tar.name, "rb") as f:
            while chunk := f.read(8192):
                sha256.update(chunk)
        bundle_hash = sha256.hexdigest()

        coord_bundles_dir = home / "bundles" / name
        coord_bundles_dir.mkdir(parents=True, exist_ok=True)
        dest = coord_bundles_dir / f"{bundle_hash}.tar.gz"

        if not dest.is_file():
            shutil.copy(temp_tar.name, dest)

        (coord_bundles_dir / "latest.txt").write_text(bundle_hash, encoding="utf-8")
        return bundle_hash, dest, files_added
    finally:
        if os.path.exists(temp_tar.name):
            os.unlink(temp_tar.name)
