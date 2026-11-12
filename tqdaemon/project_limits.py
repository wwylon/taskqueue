"""Per-project and per-set limit rows in the config table.

Project overrides: ``project_limit_<project>_<field>``.
Set defaults: ``set_limit_<path_with_/→\\x1f>_<field>``.

Effective *per-project* limits = the project's own override. Set limits are subtree
totals enforced by ``set_caps.SetCapChecker`` (bots max_tasks=4 means 4 across all bots),
except ``min_tasks``, a per-project floor that members still inherit from the nearest set.
Leaf module: db and _db_tasks both import it.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import Any

_PROJECT_LIMIT_FIELDS = (
    "max_tasks",
    "min_tasks",
    "max_cpu_cores",
    "max_ram_gib",
    "max_gpu_vram_gib",
    "max_gpu_hours_day",
)

_SET_PATH_SEP = "\x1f"  # encode slash paths inside config keys

# Only these set fields are per-member defaults; the others are subtree totals (set_caps).
_INHERITED_FROM_SET = ("min_tasks",)


def empty_limits() -> dict[str, float]:
    return {f: 0.0 for f in _PROJECT_LIMIT_FIELDS}


def encode_set_path(path: str) -> str:
    """Encode a normalized set path for use inside a config key."""
    return path.replace("/", _SET_PATH_SEP)


def decode_set_path(encoded: str) -> str:
    return encoded.replace(_SET_PATH_SEP, "/")


def _set_limit_key(path: str, field: str) -> str:
    return f"set_limit_{encode_set_path(path)}_{field}"


def get_all_project_limits(conn: sqlite3.Connection) -> dict[str, dict[str, float]]:
    """Return {project: {field: value}} for all projects that have any override set."""
    rows = conn.execute("SELECT key, value FROM config WHERE key LIKE 'project_limit_%'").fetchall()
    result: dict[str, dict[str, float]] = {}
    for r in rows:
        # key = project_limit_<project>_<field>
        suffix = r["key"][len("project_limit_") :]
        for field in _PROJECT_LIMIT_FIELDS:
            if suffix.endswith(f"_{field}"):
                project = suffix[: -(len(field) + 1)]
                if project not in result:
                    result[project] = empty_limits()
                try:
                    result[project][field] = float(json.loads(r["value"]))
                except (TypeError, ValueError):
                    pass
                break
    return result


def get_all_set_limits(conn: sqlite3.Connection) -> dict[str, dict[str, float]]:
    """Return {set_path: {field: value}} for all sets that have any default set."""
    rows = conn.execute("SELECT key, value FROM config WHERE key LIKE 'set_limit_%'").fetchall()
    result: dict[str, dict[str, float]] = {}
    for r in rows:
        suffix = r["key"][len("set_limit_") :]
        for field in _PROJECT_LIMIT_FIELDS:
            if suffix.endswith(f"_{field}"):
                encoded = suffix[: -(len(field) + 1)]
                path = decode_set_path(encoded)
                if path not in result:
                    result[path] = empty_limits()
                try:
                    result[path][field] = float(json.loads(r["value"]))
                except (TypeError, ValueError):
                    pass
                break
    return result


def get_set_limits(conn: sqlite3.Connection, path: str) -> dict[str, float]:
    """Return {field: value} for one set path; missing fields default to 0."""
    out = empty_limits()
    keys = [_set_limit_key(path, f) for f in _PROJECT_LIMIT_FIELDS]
    rows = conn.execute(
        f"SELECT key, value FROM config WHERE key IN ({','.join('?' * len(keys))})",
        keys,
    ).fetchall()
    prefix = f"set_limit_{encode_set_path(path)}_"
    for r in rows:
        field = r["key"][len(prefix) :]
        if field in out:
            try:
                out[field] = float(json.loads(r["value"]))
            except (TypeError, ValueError):
                pass
    return out


def set_set_limit(
    conn: sqlite3.Connection,
    path: str,
    field: str,
    value: float | int,
    *,
    commit: bool = True,
) -> None:
    if field not in _PROJECT_LIMIT_FIELDS:
        raise ValueError(f"Unknown set limit field: {field!r}")
    key = _set_limit_key(path, field)
    if value == 0:
        conn.execute("DELETE FROM config WHERE key = ?", (key,))
    else:
        conn.execute(
            "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
            (key, json.dumps(value)),
        )
    if commit:
        conn.commit()


# Config key families that are keyed by an encoded set path (limits, explicit tree nodes).
SET_KEY_PREFIXES = ("set_limit_", "set_node_")


def move_set_config(conn: sqlite3.Connection, old: str, new: str, *, commit: bool = True) -> int:
    """Re-key every set-path config row of ``old`` and its descendants to ``new``
    (``tq set rename`` must carry the set's limits and tree node along). Returns rows moved."""
    old_e, new_e = encode_set_path(old), encode_set_path(new)
    moved = 0
    for fam in SET_KEY_PREFIXES:
        rows = conn.execute(
            "SELECT key, value FROM config WHERE substr(key, 1, ?) = ?",
            (len(fam), fam),
        ).fetchall()
        for r in rows:
            rest = r["key"][len(fam) :]
            own = rest == old_e or rest in {f"{old_e}_{f}" for f in _PROJECT_LIMIT_FIELDS}
            if own or rest.startswith(old_e + _SET_PATH_SEP):  # the set itself or a descendant
                new_key = fam + new_e + rest[len(old_e) :]
                conn.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (new_key, r["value"]))
                conn.execute("DELETE FROM config WHERE key = ?", (r["key"],))
                moved += 1
    if commit:
        conn.commit()
    if moved:
        from tqdaemon.db import clear_config_cache

        clear_config_cache()
    return moved


def explicit_set_nodes(conn: sqlite3.Connection) -> set[str]:
    """Set paths that exist without members: created nodes (``set_node_*``) or nodes with limits."""
    rows = conn.execute("SELECT key FROM config WHERE substr(key, 1, 9) = 'set_node_'").fetchall()
    out = {decode_set_path(r["key"][len("set_node_") :]) for r in rows}
    out |= set(get_all_set_limits(conn))
    return out


def add_set_node(conn: sqlite3.Connection, path: str, *, commit: bool = True) -> None:
    conn.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, '1')", (f"set_node_{encode_set_path(path)}",))
    if commit:
        conn.commit()


def remove_set_node(conn: sqlite3.Connection, path: str, *, commit: bool = True) -> None:
    """Forget an explicit node and its limits (members keep their ``set:`` until reassigned)."""
    conn.execute("DELETE FROM config WHERE key = ?", (f"set_node_{encode_set_path(path)}",))
    for f in _PROJECT_LIMIT_FIELDS:
        conn.execute("DELETE FROM config WHERE key = ?", (_set_limit_key(path, f),))
    if commit:
        conn.commit()
    from tqdaemon.db import clear_config_cache

    clear_config_cache()


def set_path_prefixes(set_path: str | None) -> list[str]:
    """Nearest-first prefixes of a set path: ``a/b/c`` → ``[a/b/c, a/b, a]``."""
    if not set_path:
        return []
    parts = set_path.split("/")
    return ["/".join(parts[:i]) for i in range(len(parts), 0, -1)]


def resolve_effective_limits(
    override: Mapping[str, float] | None,
    set_path: str | None,
    all_set_limits: Mapping[str, Mapping[str, float]] | None = None,
    *,
    conn: sqlite3.Connection | None = None,
) -> tuple[dict[str, float], dict[str, str]]:
    """Resolve effective limits and per-field source labels.

    Source is ``project``, ``set:<path>``, or ``none`` (0 / unlimited).
    """
    ov = dict(override or {})
    effective = empty_limits()
    inherited_from: dict[str, str] = {f: "none" for f in _PROJECT_LIMIT_FIELDS}
    set_map = all_set_limits
    if set_map is None and conn is not None:
        set_map = get_all_set_limits(conn)
    set_map = set_map or {}

    for field in _PROJECT_LIMIT_FIELDS:
        ov_val = float(ov.get(field, 0) or 0)
        if ov_val != 0:
            effective[field] = ov_val
            inherited_from[field] = "project"
            continue
        if field not in _INHERITED_FROM_SET:
            continue  # set value is a subtree total, not a per-member default
        for prefix in set_path_prefixes(set_path):
            sv = float((set_map.get(prefix) or {}).get(field, 0) or 0)
            if sv != 0:
                effective[field] = sv
                inherited_from[field] = f"set:{prefix}"
                break
    return effective, inherited_from


def get_effective_project_limits(
    conn: sqlite3.Connection,
    project: str,
    set_path: str | None = None,
    *,
    all_overrides: Mapping[str, Mapping[str, float]] | None = None,
    all_set_limits: Mapping[str, Mapping[str, float]] | None = None,
) -> dict[str, float]:
    """Effective limits for one project (override beats nearest set default)."""
    from tqdaemon.db import get_project_limits

    ov = (all_overrides or {}).get(project) if all_overrides is not None else get_project_limits(conn, project)
    effective, _ = resolve_effective_limits(ov, set_path, all_set_limits, conn=conn)
    return effective


def set_paths_from_projects(projects: Mapping[str, Any] | None) -> dict[str, str | None]:
    """``{name: set_path}`` from a Project registry or ``{name: {set: ...}}`` map."""
    if not projects:
        return {}
    out: dict[str, str | None] = {}
    for name, p in projects.items():
        if isinstance(p, dict):
            raw = p.get("set") or p.get("set_path")
            out[name] = str(raw) if raw else None
        else:
            out[name] = getattr(p, "set_path", None)
    return out


def get_all_effective_project_limits(
    conn: sqlite3.Connection,
    set_paths: Mapping[str, str | None] | None = None,
    *,
    projects: Mapping[str, Any] | None = None,
) -> dict[str, dict[str, float]]:
    """Effective limits for every project that has an override, a set path, or both."""
    paths = dict(set_paths or {})
    if projects is not None:
        paths.update(set_paths_from_projects(projects))
    overrides = get_all_project_limits(conn)
    all_sets = get_all_set_limits(conn)
    names = set(overrides) | set(paths)
    result: dict[str, dict[str, float]] = {}
    for name in names:
        eff, _ = resolve_effective_limits(
            overrides.get(name),
            paths.get(name),
            all_sets,
        )
        # Keep only projects that have any non-zero effective field, matching
        # historical get_all_project_limits sparsity for min_tasks scans — but
        # include zeros for known projects so callers can index safely.
        if any(float(v or 0) != 0 for v in eff.values()) or name in overrides or name in paths:
            result[name] = eff
    return result


def build_limits_api_payload(
    conn: sqlite3.Connection,
    projects: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Shape for ``GET /api/project-limits``: projects (override/effective/source) + sets."""
    paths = set_paths_from_projects(projects)
    overrides = get_all_project_limits(conn)
    all_sets = get_all_set_limits(conn)
    names = sorted(set(paths) | set(overrides) | set(projects or {}))
    proj_out: dict[str, Any] = {}
    for name in names:
        ov = overrides.get(name) or empty_limits()
        # Ensure full field map for UI inputs
        ov_full = empty_limits()
        ov_full.update({k: float(ov.get(k, 0) or 0) for k in _PROJECT_LIMIT_FIELDS})
        eff, sources = resolve_effective_limits(ov_full, paths.get(name), all_sets)
        proj_out[name] = {
            "override": ov_full,
            "effective": eff,
            "inherited_from": sources,
            "set": paths.get(name),
        }
    sets_out: dict[str, dict[str, float]] = {}
    for path, lim in all_sets.items():
        full = empty_limits()
        full.update({k: float(lim.get(k, 0) or 0) for k in _PROJECT_LIMIT_FIELDS})
        sets_out[path] = full
    # Include empty defaults for known tree nodes with no limits yet? UI can POST to create.
    return {"projects": proj_out, "sets": sets_out}
