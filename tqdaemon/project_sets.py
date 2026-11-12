"""Project sets: multilevel slash-path tree over ``projects.yaml`` ``set:`` fields.

Selecting a node includes that path and all descendants (subtree). Membership is
YAML-only; freeze/pause remain per-project config keys.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from tqdaemon.config import Project, registry_path

_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
UNSET_SENTINEL = "__unset__"


def projects_config_entries(projects: dict[str, Project]) -> dict[str, dict[str, Any]]:
    """Serialize registry projects for ``/api/worker/projects`` / config panels."""
    res: dict[str, dict[str, Any]] = {}
    for name, p in projects.items():
        entry: dict[str, Any] = {
            "root": str(p.root),
            "queue_dir": str(p.queue_dir),
            "git_url": p.git_url,
            "remote": bool(getattr(p, "remote", False)),
            "set": p.set_path,
        }
        sync_push = getattr(p, "sync_push", ()) or ()
        if sync_push:
            entry["sync"] = {"push": list(sync_push)}
        res[name] = entry
    return res


def normalize_path(raw: str | None) -> str | None:
    """Normalize a set path. Empty / None → None. Raises ValueError if invalid."""
    if raw is None:
        return None
    s = str(raw).strip().replace("\\", "/")
    if not s:
        return None
    if s.startswith("/") or s.endswith("/") or "//" in s or ".." in s.split("/"):
        raise ValueError(f"invalid set path: {raw!r}")
    parts = s.split("/")
    for part in parts:
        if not part or not _SEGMENT_RE.match(part):
            raise ValueError(f"invalid set path segment in {raw!r}")
    return "/".join(parts)


def is_under(path: str | None, ancestor: str | None) -> bool:
    """True if ``path`` equals ``ancestor`` or is a descendant (prefix + '/')."""
    if not path or not ancestor:
        return False
    if path == ancestor:
        return True
    return path.startswith(ancestor + "/")


def projects_in_subtree(projects: dict[str, Project], node: str) -> list[str]:
    """Project names whose ``set_path`` lies under ``node`` (sorted)."""
    node_n = normalize_path(node)
    if node_n is None:
        raise ValueError("set path required")
    out = [name for name, p in projects.items() if p.set_path and is_under(p.set_path, node_n)]
    return sorted(out)


def known_paths(projects: dict[str, Project], extra: Iterable[str] = ()) -> set[str]:
    """All tree node paths implied by project membership plus explicit (possibly empty)
    nodes in ``extra``, including every prefix."""
    nodes: set[str] = set()
    sources = [p.set_path for p in projects.values() if p.set_path] + [e for e in extra if e]
    for sp in sources:
        parts = sp.split("/")
        for i in range(1, len(parts) + 1):
            nodes.add("/".join(parts[:i]))
    return nodes


def node_exists(projects: dict[str, Project], node: str) -> bool:
    try:
        n = normalize_path(node)
    except ValueError:
        return False
    if n is None:
        return False
    return n in known_paths(projects)


def build_tree(projects: dict[str, Project], extra: Iterable[str] = ()) -> dict[str, Any]:
    """Nested tree: ``{ path, name, children, direct_projects, subtree_projects, subtree_count }``.

    Roots are returned under a synthetic wrapper ``{"children": [...]}`` (no path).
    """
    # Collect direct members per exact path.
    direct: dict[str, list[str]] = {}
    for name, p in sorted(projects.items()):
        if not p.set_path:
            continue
        direct.setdefault(p.set_path, []).append(name)

    nodes = known_paths(projects, extra)

    def children_of(prefix: str | None) -> list[str]:
        depth = 0 if prefix is None else prefix.count("/") + 1
        out: list[str] = []
        for n in nodes:
            if n.count("/") != depth:
                continue
            if prefix is None:
                if "/" not in n:
                    out.append(n)
            elif n.startswith(prefix + "/") and n.count("/") == depth:
                out.append(n)
        return sorted(out)

    def subtree_projects(path: str) -> list[str]:
        return sorted(name for name, p in projects.items() if p.set_path and is_under(p.set_path, path))

    def make_node(path: str) -> dict[str, Any]:
        kids = [make_node(c) for c in children_of(path)]
        direct_here = list(direct.get(path, []))
        sub = subtree_projects(path)
        return {
            "path": path,
            "name": path.rsplit("/", 1)[-1],
            "children": kids,
            "direct_projects": direct_here,
            "subtree_projects": sub,
            "subtree_count": len(sub),
        }

    roots = [make_node(r) for r in children_of(None)]
    return {"children": roots}


def flat_nodes(projects: dict[str, Project], extra: Iterable[str] = ()) -> dict[str, dict[str, Any]]:
    """Flat map path → {direct_projects, subtree_projects, subtree_count}."""
    tree = build_tree(projects, extra)
    out: dict[str, dict[str, Any]] = {}

    def walk(node: dict[str, Any]) -> None:
        path = node["path"]
        out[path] = {
            "direct_projects": list(node["direct_projects"]),
            "subtree_projects": list(node["subtree_projects"]),
            "subtree_count": int(node["subtree_count"]),
        }
        for child in node.get("children") or []:
            walk(child)

    for root in tree.get("children") or []:
        walk(root)
    return out


def _project_block_span(text: str, name: str) -> tuple[int, int] | None:
    """Return [start, end) char span of the indented block for ``name`` under projects."""
    # Match "  name:" then following lines that are more indented (4+ spaces) or blank.
    header = re.compile(rf"^  {re.escape(name)}:\s*(?:\n|$)", re.MULTILINE)
    m = header.search(text)
    if m is None:
        return None
    start = m.start()
    # End at next top-level project key (two spaces + name + colon) or EOF.
    rest = text[m.end() :]
    next_proj = re.search(r"^  [^\s#][^:\n]*:\s*(?:\n|$)", rest, re.MULTILINE)
    end = m.end() + (next_proj.start() if next_proj else len(rest))
    return start, end


def set_project_set(home: Path, name: str, set_path: str | None) -> str | None:
    """Set or clear ``set:`` for ``name`` in projects.yaml. Returns previous path (or None).

    Surgical edit preserving comments. Raises KeyError if project missing,
    ValueError if path invalid or file malformed.
    """
    path_n = normalize_path(set_path) if set_path is not None else None
    reg = registry_path(home)
    if not reg.is_file():
        raise KeyError(f"projects.yaml missing; cannot assign set for {name}")
    text = reg.read_text(encoding="utf-8")
    span = _project_block_span(text, name)
    if span is None:
        raise KeyError(f"project {name!r} not found in projects.yaml")
    start, end = span
    block = text[start:end]
    # Find existing set: line inside the block (4-space indent typical).
    set_line = re.compile(r"^(    set:\s*)(.+)$", re.MULTILINE)
    m = set_line.search(block)
    prev: str | None = None
    if m is not None:
        prev_raw = m.group(2).strip().strip("\"'")
        try:
            prev = normalize_path(prev_raw) if prev_raw else None
        except ValueError:
            prev = prev_raw or None
        if path_n is None:
            # Remove the set: line (and its trailing newline if present).
            line_start = m.start()
            line_end = m.end()
            if line_end < len(block) and block[line_end] == "\n":
                line_end += 1
            new_block = block[:line_start] + block[line_end:]
        else:
            new_block = set_line.sub(lambda _m: _m.group(1) + path_n, block, count=1)
    else:
        if path_n is None:
            return None  # already unset
        # Insert set: after the header line (first line of block).
        nl = block.find("\n")
        if nl < 0:
            new_block = block + f"\n    set: {path_n}\n"
        else:
            new_block = block[: nl + 1] + f"    set: {path_n}\n" + block[nl + 1 :]
    reg.write_text(text[:start] + new_block + text[end:], encoding="utf-8")
    return prev


def unset_projects(projects: dict[str, Project]) -> list[str]:
    """Registered projects with no ``set_path`` (sorted)."""
    return sorted(name for name, p in projects.items() if not p.set_path)


def resolve_set_scope(
    projects: dict[str, Project],
    *,
    set_q: str | None,
    project_q: str | None,
    projects_q: str | None = None,
) -> tuple[str | None, list[str] | None, str | None]:
    """Resolve ``?set=`` + ``?project=`` / ``?projects=`` into a single project or IN-list.

    Returns ``(project, projects_list, error)``. Exactly one of ``project`` /
    ``projects_list`` is set when filtering; both None when unscoped.
    ``projects_list`` may be empty (match nothing).

    ``projects_q`` is a comma-separated multi-select (Queue chips). When both
    ``project_q`` and ``projects_q`` are set, ``projects_q`` wins.
    """
    multi = [p.strip() for p in str(projects_q or "").split(",") if p.strip()]
    proj = None if multi else ((project_q or "").strip() or None)
    raw_set = (set_q or "").strip() or None

    members: list[str] | None = None
    if raw_set:
        if raw_set == UNSET_SENTINEL:
            members = unset_projects(projects)
        else:
            try:
                node = normalize_path(raw_set)
            except ValueError as exc:
                return None, None, str(exc)
            assert node is not None
            if not node_exists(projects, node):
                return None, None, f"unknown set path: {node}"
            members = projects_in_subtree(projects, node)

    if multi:
        known = set(projects)
        bad = [n for n in multi if n not in known]
        if bad:
            return None, None, f"unknown projects: {', '.join(bad)}"
        if members is not None:
            allow = set(members)
            bad_scope = [n for n in multi if n not in allow]
            if bad_scope:
                return None, None, f"projects not in set scope {raw_set!r}: {', '.join(bad_scope)}"
        return None, multi, None

    if not raw_set:
        return proj, None, None
    assert members is not None
    if proj:
        if proj not in members:
            return None, None, f"project {proj!r} is not in set scope {raw_set!r}"
        return proj, None, None
    return None, members, None


def rename_set_prefix(home: Path, old_prefix: str, new_prefix: str) -> list[str]:
    """Rewrite every project ``set:`` equal to old or under old/. Returns affected names.

    Raises ValueError if paths invalid or new would create illegal paths.
    """
    old_n = normalize_path(old_prefix)
    new_n = normalize_path(new_prefix)
    if old_n is None or new_n is None:
        raise ValueError("old and new set paths are required")
    if old_n == new_n:
        return []
    # Refuse renaming into a descendant of itself.
    if is_under(new_n, old_n):
        raise ValueError(f"cannot rename {old_n!r} into its own descendant {new_n!r}")

    from tqdaemon.config import load_projects

    projects = load_projects(home)
    affected: list[str] = []
    for name, p in sorted(projects.items()):
        if not p.set_path:
            continue
        if p.set_path == old_n:
            set_project_set(home, name, new_n)
            affected.append(name)
        elif p.set_path.startswith(old_n + "/"):
            suffix = p.set_path[len(old_n) :]
            set_project_set(home, name, new_n + suffix)
            affected.append(name)
    _move_set_config_in_db(home, old_n, new_n)
    return affected


def _move_set_config_in_db(home: Path, old_n: str, new_n: str) -> None:
    """Limits and explicit tree nodes live in the DB config keyed by set path: move them too."""
    from tqdaemon.config import db_path
    from tqdaemon.db import open_db
    from tqdaemon.project_limits import move_set_config

    path = db_path(home)
    if not path.exists():
        return
    conn = open_db(path)
    try:
        move_set_config(conn, old_n, new_n)
    finally:
        conn.close()
