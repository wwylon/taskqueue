"""Docs drift guard: every MCP tool / tq command is documented (scripts/check_docs.py)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

from taskqueue import mcp_server

_SPEC = importlib.util.spec_from_file_location(
    "check_docs", Path(__file__).resolve().parent.parent / "scripts" / "check_docs.py"
)
check_docs = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check_docs)


def test_surface_is_documented() -> None:
    assert check_docs.check_surface_documented() == []


def test_new_undocumented_tool_is_flagged(monkeypatch) -> None:
    monkeypatch.setattr(
        mcp_server, "TOOLS", [*mcp_server.TOOLS, {"name": "brand_new_tool", "inputSchema": {}}]
    )
    errs = check_docs.check_surface_documented()
    assert errs == ["MCP tool 'brand_new_tool' is not documented in docs/mcp.md"]
