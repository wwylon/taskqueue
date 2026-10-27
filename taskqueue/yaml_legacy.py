"""Legacy operator_queue YAML key hints and mechanical rewrites.

W6.12 rejects unknown top-level keys. Older YAMLs sometimes used invent-your-own
names (``ram_gib``, ``timeout_s``, ``hypothesis``, ``project``, ``name``).
This module maps those to the canonical schema for error hints and
``tq validate --fix`` — it does **not** soft-accept them at parse time.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# Shown when parse rejects these keys (instead of suggesting x-ram_gib etc.).
LEGACY_KEY_HINTS: dict[str, str] = {
    "ram_gib": "move under resources: (resources.ram_gib)",
    "timeout_s": "rename to wall_clock_cap_sec",
    "hypothesis": "rename to x-hypothesis (or put under metadata:)",
    "project": "omit — project comes from registration / queue dir",
    "name": "rename to description (or x-name if description is set)",
}


def suggest_unknown_key(uk: str, known_keys: frozenset[str]) -> str:
    """One-line hint for an unknown top-level YAML key."""
    if uk in LEGACY_KEY_HINTS:
        return f"  '{uk}' — {LEGACY_KEY_HINTS[uk]}"
    import difflib

    close = difflib.get_close_matches(uk, known_keys, n=1, cutoff=0.6)
    if close:
        return f"  '{uk}' — did you mean '{close[0]}'?"
    return f"  '{uk}' — unrecognised key (use 'x-{uk}' or 'metadata:' to pass custom data)"


def fix_legacy_keys(
    raw: dict[str, Any],
    *,
    project_name: str,
) -> tuple[dict[str, Any], list[str]]:
    """Rewrite known legacy top-level keys in a loaded YAML mapping.

    Returns ``(new_mapping, change_descriptions)``. Raises ``ValueError`` when
    ``project:`` is present and does not match ``project_name``.
    """
    if not isinstance(raw, dict):
        raise ValueError("YAML root must be a mapping")
    out: dict[str, Any] = dict(raw)
    changes: list[str] = []

    if "project" in out:
        val = str(out["project"])
        if val != project_name:
            raise ValueError(
                f"project: {val!r} does not match registered project {project_name!r}; "
                "fix manually or rename the registration"
            )
        del out["project"]
        changes.append(f"dropped project: {val!r} (matches registered name)")

    if "ram_gib" in out:
        ram = out.pop("ram_gib")
        res = dict(out.get("resources") or {})
        if "ram_gib" not in res:
            res["ram_gib"] = ram
            changes.append(f"moved ram_gib → resources.ram_gib ({ram})")
        else:
            changes.append(
                f"dropped top-level ram_gib ({ram}); resources.ram_gib already set"
            )
        out["resources"] = res

    if "timeout_s" in out:
        t = out.pop("timeout_s")
        if "wall_clock_cap_sec" not in out:
            out["wall_clock_cap_sec"] = t
            changes.append(f"renamed timeout_s → wall_clock_cap_sec ({t})")
        else:
            changes.append(
                f"dropped timeout_s ({t}); wall_clock_cap_sec already set"
            )

    if "hypothesis" in out:
        h = out.pop("hypothesis")
        if "x-hypothesis" not in out:
            out["x-hypothesis"] = h
            changes.append("renamed hypothesis → x-hypothesis")
        else:
            changes.append("dropped hypothesis; x-hypothesis already set")

    if "name" in out:
        n = out.pop("name")
        if not out.get("description"):
            out["description"] = n
            changes.append("renamed name → description")
        else:
            out["x-name"] = n
            changes.append("moved name → x-name (description already set)")

    return out, changes


def fix_legacy_yaml_file(path: Path, *, project_name: str) -> list[str]:
    """Load, rewrite, and write ``path`` if any legacy keys change.

    Uses PyYAML dump (may drop comments). Returns the list of change strings;
    empty when the file needed no rewrite.
    """
    import yaml

    text = path.read_text(encoding="utf-8")
    docs = list(yaml.safe_load_all(text))
    if len(docs) > 1:
        raise ValueError(
            f"{path.name}: legacy fixer does not support multi-document YAML "
            f"({len(docs)} documents separated by '---')"
        )
    raw = docs[0] if docs else None
    if not isinstance(raw, dict):
        raise ValueError(f"{path.name}: YAML root must be a mapping")
    new_raw, changes = fix_legacy_keys(raw, project_name=project_name)
    if not changes:
        return []
    path.write_text(
        yaml.dump(
            new_raw,
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return changes
