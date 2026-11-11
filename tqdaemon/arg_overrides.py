"""Merge CLI/MCP ``--set`` / ``set`` overrides into a task's base argv.

CLI historically merges into the full argv list; MCP used to pass a delta that
the runner treated as the entire tail (F-195). One helper keeps both paths
aligned.
"""

from __future__ import annotations

from typing import Any


def merge_arg_overrides(base_args: list[str], overrides: list[str]) -> list[str]:
    """Return a new argv list with each override applied to ``base_args``.

    Each override is ``--flag`` or ``--flag=value``. Matching base entries
    (bare flag or ``flag=…``) are replaced; otherwise the override is appended.
    """
    result = list(base_args)
    for override in overrides:
        if not override:
            continue
        flag_name = override.split("=", 1)[0]
        replaced = False
        i = 0
        while i < len(result):
            if result[i] == flag_name:
                if "=" in override:
                    result[i] = override
                    if i + 1 < len(result) and not result[i + 1].startswith("-"):
                        result.pop(i + 1)
                replaced = True
                break
            if result[i].startswith(flag_name + "="):
                result[i] = override
                replaced = True
                break
            i += 1
        if not replaced:
            result.append(override)
    return result


def overrides_from_set_dict(set_overrides: dict[str, Any]) -> list[str]:
    """Convert MCP ``set`` map to ``--flag=value`` (or ``--flag``) strings."""
    out: list[str] = []
    for k, v in (set_overrides or {}).items():
        key = str(k)
        if not key.startswith("-"):
            key = f"--{key.lstrip('-')}" if key else key
        if v is True or v is None:
            out.append(key)
        elif v is False:
            continue
        else:
            out.append(f"{key}={v}")
    return out
