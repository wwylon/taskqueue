"""MCP error shapes: tool failures are isError results; bad JSON gets -32700."""
import io
import json

from taskqueue import mcp_server


def _run(monkeypatch, tmp_path, lines):
    out = []
    monkeypatch.setattr(mcp_server, "_emit", out.append)
    monkeypatch.setattr("sys.stdin", io.StringIO("\n".join(lines) + "\n"))
    mcp_server.run_stdio(tmp_path)
    return out


def test_parse_error_and_invalid_request(monkeypatch, tmp_path):
    out = _run(monkeypatch, tmp_path, ["{not json", "[1,2]"])
    assert [m["error"]["code"] for m in out] == [-32700, -32600]


def test_unknown_tool_and_missing_args(monkeypatch, tmp_path):
    req = next(t for t in mcp_server.TOOLS if (t.get("inputSchema") or {}).get("required"))
    out = _run(monkeypatch, tmp_path, [
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "nope"}}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": req["name"], "arguments": {}}}),
    ])
    assert out[0]["error"]["code"] == -32602
    assert out[1]["result"]["isError"] is True and "missing required" in out[1]["result"]["content"][0]["text"]


def test_dispatch_table_matches_tool_schemas():
    assert set(mcp_server._HANDLERS) == {t["name"] for t in mcp_server.TOOLS}
