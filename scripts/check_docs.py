#!/usr/bin/env python3
"""Validate documentation coherence — manifest, locales, generated artifacts."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tqdaemon.docs_loader import (  # noqa: E402
    load_manifest,
    static_dir,
    validate_manifest,
)

LOCALES = ROOT / "tqdaemon" / "static" / "locales"

# Ratchet: `tq` subcommands that predate the README-coverage check. Do not add to this
# list -- document new commands in README.md instead.
LEGACY_UNDOCUMENTED_CLI = frozenset(
    {
        "describe",
        "submit",
        "suspend",
        "my-tasks",
        "resync",
        "fair-share",
        "sleep",
        "wake",
        "master-status",
        "master-migrate",
        "usage",
        "prune",
    }
)

SURFACE_MAP = ROOT / "docs" / "surface_map.json"


def load_surface_map() -> dict:
    try:
        return json.loads(SURFACE_MAP.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"_error": f"cannot read docs/surface_map.json: {exc}"}


def check_surface_documented() -> list[str]:
    """Every live MCP tool and CLI command has a capability-map entry."""
    from tqdaemon import cli, mcp_server

    errors: list[str] = []
    mcp_doc = (ROOT / "docs" / "mcp.md").read_text(encoding="utf-8")
    surface = load_surface_map()
    if "_error" in surface:
        return [surface["_error"]]
    mapped_mcp = {name for item in surface.get("capabilities", {}).values() for name in item.get("mcp", [])}
    mapped_cli = {name for item in surface.get("capabilities", {}).values() for name in item.get("cli", [])}
    for tool in mcp_server.TOOLS:
        if f"`{tool['name']}`" not in mcp_doc:
            errors.append(f"MCP tool {tool['name']!r} is not documented in docs/mcp.md")
        elif tool["name"] not in mapped_mcp:
            errors.append(f"MCP tool {tool['name']!r} is missing from docs/surface_map.json")
    for cmd in cli.COMMANDS:
        if cmd not in mapped_cli:
            errors.append(f"tq command {cmd!r} is missing from docs/surface_map.json")
    for name in sorted(mapped_mcp - {tool["name"] for tool in mcp_server.TOOLS}):
        errors.append(f"surface_map lists unknown MCP tool {name!r}")
    for name in sorted(mapped_cli - set(cli.COMMANDS)):
        errors.append(f"surface_map lists unknown tq command {name!r}")
    return errors


def main() -> int:
    errors = validate_manifest()
    errors += check_surface_documented()
    manifest = load_manifest()

    for loc in LOCALES.glob("*.json"):
        json.loads(loc.read_text(encoding="utf-8"))

    bundle = static_dir() / "help-bundle.en.json"
    labels = static_dir() / "error_labels.json"
    if not SURFACE_MAP.is_file():
        errors.append("missing docs/surface_map.json")
    if not bundle.is_file():
        errors.append("missing tqdaemon/static/help-bundle.en.json — run scripts/build_docs.py")
    if not labels.is_file():
        errors.append("missing tqdaemon/static/error_labels.json — run scripts/build_docs.py")

    if errors:
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(f"OK: {len(manifest.get('topics', {}))} topics, {len(manifest.get('guides', {}))} guides")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
