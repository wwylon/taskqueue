"""Allowlisted project file sync helpers."""
from __future__ import annotations

import os
from pathlib import Path

from taskqueue.project_sync import (
    build_manifest,
    bump_sync_generation,
    compute_fingerprint,
    diff_manifest,
    get_sync_generation,
    path_matches_allowlist,
    resolve_allowed_file,
)


def test_allowlist_and_hard_exclude(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.cpp").write_text("x", encoding="utf-8")
    (root / "out").mkdir()
    (root / "out" / "a.obj").write_text("y", encoding="utf-8")
    (root / "bin").mkdir()
    (root / "bin" / "tool.exe").write_bytes(b"z")

    assert path_matches_allowlist("src/a.cpp", ["src/**"])
    assert not path_matches_allowlist("out/a.obj", ["out/**"])  # hard exclude
    assert not path_matches_allowlist("bin/tool.exe", ["bin/**"])

    man = build_manifest(root, ["src/**", "out/**", "bin/**"])
    paths = {e["path"] for e in man}
    assert paths == {"src/a.cpp"}


def test_fingerprint_stable_then_changes(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    (root / "scripts").mkdir(parents=True)
    f = root / "scripts" / "run.py"
    f.write_text("print(1)\n", encoding="utf-8")
    fp1, _ = compute_fingerprint(root, ["scripts/**"])
    fp2, _ = compute_fingerprint(root, ["scripts/**"])
    assert fp1 == fp2
    f.write_text("print(2)\n", encoding="utf-8")
    st = f.stat()
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000))  # same-size rewrite: force a distinct mtime
    fp3, _ = compute_fingerprint(root, ["scripts/**"])
    assert fp3 != fp1


def test_diff_manifest() -> None:
    have = [{"path": "a.txt", "size": 1, "mtime_ns": 10}]
    want = [
        {"path": "a.txt", "size": 1, "mtime_ns": 10},
        {"path": "b.txt", "size": 2, "mtime_ns": 20},
    ]
    d = diff_manifest(have, want)
    assert [e["path"] for e in d] == ["b.txt"]


def test_resolve_allowed_blocks_escape(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "ok.c").write_text("1", encoding="utf-8")
    assert resolve_allowed_file(root, "src/ok.c", ["src/**"]) is not None
    assert resolve_allowed_file(root, "../ok.c", ["src/**"]) is None
    assert resolve_allowed_file(root, "out/x", ["out/**"]) is None


def test_sync_generation_bump(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    assert get_sync_generation(home, "p") == 0
    assert bump_sync_generation(home, "p") == 1
    assert bump_sync_generation(home, "p") == 2
    assert get_sync_generation(home, "p") == 2
