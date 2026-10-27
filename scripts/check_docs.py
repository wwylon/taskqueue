#!/usr/bin/env python3
"""Validate documentation coherence — manifest, locales, generated artifacts."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from taskqueue.docs_loader import (  # noqa: E402
    load_manifest,
    validate_manifest,
    static_dir,
)

LOCALES = ROOT / "taskqueue" / "static" / "locales"

# Ratchet: `tq` subcommands that predate the README-coverage check. Do not add to this
# list -- document new commands in README.md instead.
LEGACY_UNDOCUMENTED_CLI = frozenset({
    "describe", "submit", "suspend", "my-tasks", "resync", "fair-share", "sleep", "wake",
    "master-status", "master-migrate", "usage", "prune",
})


def check_surface_documented() -> list[str]:
    """Every MCP tool must be in docs/mcp.md and every `tq` command in README.md."""
    import re

    from taskqueue import cli, mcp_server

    errors: list[str] = []
    mcp_doc = (ROOT / "docs" / "mcp.md").read_text(encoding="utf-8")
    for tool in mcp_server.TOOLS:
        if f"`{tool['name']}`" not in mcp_doc:
            errors.append(f"MCP tool {tool['name']!r} is not documented in docs/mcp.md")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for cmd in cli.COMMANDS:
        if cmd in LEGACY_UNDOCUMENTED_CLI:
            continue
        if not re.search(r"tq " + re.escape(cmd) + r"\b", readme):
            errors.append(f"tq command {cmd!r} is not documented in README.md")
    return errors


def main() -> int:
    errors = validate_manifest()
    errors += check_surface_documented()
    manifest = load_manifest()

    for loc in LOCALES.glob("*.json"):
        json.loads(loc.read_text(encoding="utf-8"))

    bundle = static_dir() / "help-bundle.en.json"
    labels = static_dir() / "error_labels.json"
    if not bundle.is_file():
        errors.append("missing taskqueue/static/help-bundle.en.json — run scripts/build_docs.py")
    if not labels.is_file():
        errors.append("missing taskqueue/static/error_labels.json — run scripts/build_docs.py")

    if errors:
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(f"OK: {len(manifest.get('topics', {}))} topics, {len(manifest.get('guides', {}))} guides")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
