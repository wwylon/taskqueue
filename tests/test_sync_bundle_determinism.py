from __future__ import annotations

import time

from taskqueue.bundle import create_project_bundle
from taskqueue.config import Project
from taskqueue.project_sync import _posix, path_matches_allowlist, resolve_allowed_file


def test_posix_keeps_leading_dot_of_dotfiles():
    assert _posix(".github/workflows/ci.yml") == ".github/workflows/ci.yml"
    assert _posix("./src/a.py") == "src/a.py"
    assert _posix(".\\src\\a.py") == "src/a.py"


def test_allowlist_matches_dot_directories(tmp_path):
    assert path_matches_allowlist(".github/workflows/ci.yml", [".github/**"])
    assert not path_matches_allowlist("github/workflows/ci.yml", [".github/**"])
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "x.yml").write_text("a")
    assert resolve_allowed_file(tmp_path, ".github/x.yml", [".github/**"]) is not None


def _project(root):
    return Project(name="p", root=root, queue_dir=root / "q") if "queue_dir" in Project.__dataclass_fields__ else Project(name="p", root=root)


def test_bundle_hash_is_reproducible(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.py").write_text("print(1)\n")
    (root / "b.py").write_text("print(2)\n")
    h1, _, _ = create_project_bundle(tmp_path / "home", _project(root))
    time.sleep(1.1)
    h2, _, _ = create_project_bundle(tmp_path / "home", _project(root))
    assert h1 == h2
