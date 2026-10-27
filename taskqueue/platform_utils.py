"""Cross-platform utilities shared between local runner and remote worker slot."""
from __future__ import annotations

import os
import shutil
from pathlib import Path


def resolve_command(name_or_relpath: str, project_root: Path) -> Path:
    """Resolve a command name or project-relative path to an executable Path.

    Resolution order:
    1. Bare Python aliases → .venv lookup (both platforms), then shutil.which
    2. Project-relative or absolute path → try as-is, then + .exe on Windows
    3. Bare name → shutil.which
    """
    if not name_or_relpath:
        return Path(name_or_relpath)

    cmd = name_or_relpath.strip()

    # Python alias resolution — prefer venv on current platform
    if cmd in ("python", "python.exe", "python3", "python3.exe"):
        if os.name == "nt":
            candidates = [
                project_root / ".venv" / "Scripts" / "python.exe",
                project_root / ".venv" / "bin" / "python",
            ]
        else:
            candidates = [
                project_root / ".venv" / "bin" / "python",
                project_root / ".venv" / "Scripts" / "python.exe",
            ]
        for c in candidates:
            if c.is_file():
                return c
        # Fall through to shutil.which
        found = shutil.which(cmd)
        return Path(found) if found else Path(cmd)

    # Path with separators or absolute → resolve against project root
    p = Path(cmd)
    if p.is_absolute():
        if os.name == "nt" and not p.suffix and not p.is_file():
            with_exe = p.with_suffix(".exe")
            if with_exe.is_file():
                return with_exe
        return p

    if "/" in cmd or "\\" in cmd:
        # Project-relative
        resolved = project_root / p
        if resolved.is_file():
            return resolved
        if os.name == "nt":
            with_exe = resolved.with_suffix(".exe")
            if with_exe.is_file():
                return with_exe
        return resolved

    # Bare name → which
    found = shutil.which(cmd)
    return Path(found) if found else Path(cmd)


def pythonw_if_available(python_path: Path) -> Path:
    """On Windows, swap python.exe → pythonw.exe for windowless GUI subsystem."""
    if (
        os.name == "nt"
        and python_path.suffix.lower() == ".exe"
        and python_path.stem.lower() == "python"
    ):
        pw = python_path.with_name("pythonw.exe")
        if pw.is_file():
            return pw
    return python_path


def resolve_path(
    path_str: str,
    project_root: Path,
    shared_roots: dict[str, str] | None = None,
) -> Path:
    """Resolve a YAML path string to an absolute Path.

    Supports a ``shared:<name>/rest`` prefix that resolves against
    ``shared_roots[name]`` from worker.yaml / projects.yaml.
    """
    if shared_roots and path_str.startswith("shared:"):
        rest = path_str[len("shared:"):]
        name, _, tail = rest.partition("/")
        if name in shared_roots:
            return Path(shared_roots[name]) / tail
    p = Path(path_str)
    if p.is_absolute():
        return p
    return project_root / p
