"""Safe tar extract (path traversal / zip-slip resistant)."""

from __future__ import annotations

import tarfile
from pathlib import Path


def _is_within(dest: Path, target: Path) -> bool:
    try:
        target.relative_to(dest)
        return True
    except ValueError:
        return False


def assert_safe_member(dest: Path, member: tarfile.TarInfo) -> None:
    """Raise ValueError if member would escape dest."""
    dest_r = dest.resolve()
    # Normalize member name (no absolute / drive paths)
    name = member.name.replace("\\", "/")
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        raise ValueError(f"unsafe tar member (absolute): {member.name!r}")
    if ".." in Path(name).parts:
        raise ValueError(f"unsafe tar member (..): {member.name!r}")
    target = (dest_r / name).resolve()
    if not _is_within(dest_r, target) and target != dest_r:
        raise ValueError(f"unsafe tar member (escape): {member.name!r}")
    # F-018: validate linkname for symlinks and hardlinks
    if member.issym() or member.islnk():
        lname = (member.linkname or "").replace("\\", "/")
        if lname.startswith("/") or (len(lname) > 1 and lname[1] == ":"):
            raise ValueError(f"unsafe tar member linkname (absolute): {member.linkname!r}")
        if ".." in Path(lname).parts if lname else False:
            raise ValueError(f"unsafe tar member linkname (..): {member.linkname!r}")


MAX_MEMBERS = 200_000
MAX_TOTAL_BYTES = 64 * 1024**3


def safe_extract(
    tar: tarfile.TarFile,
    dest: Path,
    *,
    max_members: int = MAX_MEMBERS,
    max_total_bytes: int = MAX_TOTAL_BYTES,
) -> None:
    """Extract all members under dest; reject path traversal and archive bombs."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    members = tar.getmembers()
    if len(members) > max_members:
        raise ValueError(f"tar has too many members ({len(members)} > {max_members})")
    total = 0
    for member in members:
        assert_safe_member(dest, member)
        if member.isfile():
            total += max(member.size, 0)
            if total > max_total_bytes:
                raise ValueError(f"tar uncompressed size exceeds {max_total_bytes} bytes")
    # Python 3.12+: data filter blocks special files; still validate paths above.
    kwargs: dict = {"path": str(dest)}
    if hasattr(tarfile, "data_filter"):
        kwargs["filter"] = "data"
    tar.extractall(**kwargs)

