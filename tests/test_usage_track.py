"""api_usage: aggregated interaction counters (HTTP + MCP), retention, disable switch."""
from __future__ import annotations

from pathlib import Path

import pytest

from taskqueue import config as tqconfig
from taskqueue import mcp_server
from taskqueue import usage_track as ut
from taskqueue.db import open_db, set_config
from tests.helpers.master_worker import http_request, make_handler_env


@pytest.fixture(autouse=True)
def _fresh_recorder():
    ut.RECORDER._acc.clear()
    ut.RECORDER.enabled = True
    yield
    ut.RECORDER._acc.clear()
    ut.RECORDER.enabled = True


def _rows(db):
    return [dict(r) for r in db.execute("SELECT * FROM api_usage ORDER BY op, channel")]


def test_schema_and_config_keys(tmp_path: Path) -> None:
    db = open_db(tmp_path / "q.db")
    cols = {r[1] for r in db.execute("PRAGMA table_info(api_usage)")}
    assert {"bucket_ts", "channel", "client", "operator", "project", "op", "error_code",
            "calls", "total_ms", "max_ms"} <= cols
    assert "usage_tracking" in tqconfig.VALID_CONFIG_KEYS
    assert "retention_usage_days" in tqconfig.VALID_CONFIG_KEYS
    db.close()


def test_recorder_aggregates_and_flush_upserts(tmp_path: Path) -> None:
    db = open_db(tmp_path / "q.db")
    for ms in (10, 30):
        ut.RECORDER.record("cli", "GET /api/status", client="tq-cli/1", operator="ada", ms=ms)
    ut.RECORDER.record("cli", "GET /api/status", client="tq-cli/1", operator="ada", ms=5, error_code="http.500")
    assert ut.RECORDER.flush(db) == 2
    ut.RECORDER.record("cli", "GET /api/status", client="tq-cli/1", operator="ada", ms=50)
    ut.RECORDER.flush(db)
    ok = db.execute("SELECT calls, total_ms, max_ms FROM api_usage WHERE error_code = ''").fetchone()
    assert (ok["calls"], ok["total_ms"], ok["max_ms"]) == (3, 90, 50)
    assert db.execute("SELECT calls FROM api_usage WHERE error_code = 'http.500'").fetchone()["calls"] == 1
    db.close()


def test_cardinality_cap_and_disable(tmp_path: Path, monkeypatch) -> None:
    db = open_db(tmp_path / "q.db")
    monkeypatch.setattr(ut, "MAX_KEYS", 3)
    for i in range(10):
        ut.RECORDER.record("http", f"GET /x{i}")
    assert len(ut.RECORDER._acc) <= 4  # 3 distinct + the collapsed "other"
    ut.RECORDER.flush(db)
    assert sum(r["calls"] for r in _rows(db)) == 10
    ut.RECORDER.enabled = False
    ut.RECORDER.record("http", "GET /off")
    assert not ut.RECORDER._acc
    db.close()


def test_flush_failure_keeps_data(tmp_path: Path) -> None:
    db = open_db(tmp_path / "q.db")
    db.execute("DROP TABLE api_usage")
    ut.RECORDER.record("cli", "GET /api/status")
    assert ut.RECORDER.flush(db) == 0
    assert sum(v[0] for v in ut.RECORDER._acc.values()) == 1  # merged back
    db.close()


def test_route_template_and_project() -> None:
    assert ut.route_template("POST", "/api/stop/myproj/T1") == ("/api/stop/<project>/<task_id>", "myproj")
    assert ut.route_template("GET", "/api/status") == ("/api/status", "")
    assert ut.route_template("GET", "/api/nope/xyz") == ("other", "")
    assert ut.error_for_status(404) == "http.404" and ut.error_for_status(200) == ""


def test_http_requests_are_counted_with_identity(tmp_path: Path) -> None:
    HandlerClass, db, _ = make_handler_env(tmp_path)
    http_request(HandlerClass, "GET", "/api/status",
                 extra_headers={"X-TQ-Via": "api", "X-TQ-Operator": "ada", "X-TQ-Client": "tq-cli/0.7"})
    http_request(HandlerClass, "GET", "/api/status", extra_headers={"X-TQ-Via": "mcp"})  # MCP counts itself
    http_request(HandlerClass, "GET", "/api/status", extra_headers={"X-TQ-Via": "cli"})  # so does the CLI
    http_request(HandlerClass, "GET", "/api/status", extra_headers={"Sec-Fetch-Site": "same-origin"})
    ut.RECORDER.flush(db)
    rows = [r for r in _rows(db) if r["op"] == "GET /api/status"]
    by_channel = {r["channel"]: r for r in rows}
    assert set(by_channel) == {"api", "ui"}
    assert (by_channel["api"]["operator"], by_channel["api"]["client"]) == ("ada", "tq-cli/0.7")
    assert by_channel["ui"]["client"] == "browser"
    db.close()


def test_mcp_calls_are_counted_without_arguments(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("TASKQUEUE_HOME", str(tmp_path))
    db = open_db(tqconfig.db_path(tmp_path))
    mcp_server._CLIENT_INFO["name"] = "mcp/cursor 1.0"
    mcp_server._run_tool_call(tmp_path, 1, {"name": "queue_status", "arguments": {"project": "p"}})
    mcp_server._run_tool_call(tmp_path, 2, {"name": "nope", "arguments": {"secret": "hunter2"}})
    capsys.readouterr()
    rows = {r["op"]: r for r in _rows(db)}
    assert rows["queue_status"]["channel"] == "mcp" and rows["queue_status"]["client"] == "mcp/cursor 1.0"
    assert rows["queue_status"]["project"] == "p"
    assert rows["nope"]["error_code"] == "mcp.error"
    assert "hunter2" not in repr(_rows(db))
    set_config(db, "usage_tracking", 0)
    mcp_server._run_tool_call(tmp_path, 3, {"name": "queue_status", "arguments": {}})
    assert db.execute("SELECT SUM(calls) AS n FROM api_usage WHERE op='queue_status'").fetchone()["n"] == 1
    mcp_server._CLIENT_INFO["name"] = "mcp"
    db.close()


def test_prune_honours_retention(tmp_path: Path) -> None:
    db = open_db(tmp_path / "q.db")
    db.execute("INSERT INTO api_usage (bucket_ts, channel, op, calls) VALUES ('2020-01-01T00:00:00Z','cli','old',1)")
    ut.RECORDER.record("cli", "new")
    ut.RECORDER.flush(db)
    assert ut.prune_api_usage(db, 400) == 1
    assert [r["op"] for r in _rows(db)] == ["new"]
    db.close()


def test_cli_invocations_are_counted(tmp_path: Path, monkeypatch) -> None:
    from taskqueue import cli

    monkeypatch.setenv("TQ_OPERATOR", "ada")
    db = open_db(tqconfig.db_path(tmp_path))
    cli._record_cli_call(tmp_path, "list", ["--project", "p"], 0.0, 0)
    cli._record_cli_call(tmp_path, "retry", ["p/x"], 0.0, 1)
    cli._record_cli_call(tmp_path, "mcp", [], 0.0, 0)  # long-running server: skipped
    rows = {r["op"]: r for r in _rows(db)}
    assert set(rows) == {"tq list", "tq retry"}
    assert rows["tq list"]["project"] == "p" and rows["tq list"]["operator"] == "ada"
    assert rows["tq retry"]["error_code"] == "exit.1"
    db.close()


def test_cli_wait_project_and_outcome_codes(tmp_path: Path) -> None:
    from taskqueue import cli
    from taskqueue.agent_stats import usage_by

    db = open_db(tqconfig.db_path(tmp_path))
    cli._record_cli_call(tmp_path, "wait", ["p/a", "p/b", "--timeout", "60"], 0.0, 0)
    cli._record_cli_call(tmp_path, "wait", ["p/a", "--timeout=5"], 0.0, 2)
    cli._record_cli_call(tmp_path, "wait", ["--project", "p", "--tag", "x/y"], 0.0, 1)
    cli._record_cli_call(tmp_path, "wait", ["--downstream", "q/root"], 0.0, 0)
    cli._record_cli_call(tmp_path, "wait", ["p/a", "q/b"], 0.0, 3)
    cli._record_cli_call(tmp_path, "wait", ["p/a", "--raw-exit"], 0.0, 124)
    cli._record_cli_call(tmp_path, "wait", ["p/a", "--raw-exit"], 0.0, 7)
    got = sorted((r["project"], r["error_code"], r["calls"]) for r in _rows(db))
    assert got == [
        ("(multi)", "exit.3", 1),  # bad args / not found stays an error
        ("p", "", 1),
        ("p", "wait.task_failed", 2),
        ("p", "wait.timeout", 2),
        ("q", "", 1),
    ]
    errs = {(r["project"], r["error_code"]): r["errors"] for r in usage_by(db, by="project,error_code")}
    assert errs[("p", "wait.timeout")] == 0 and errs[("p", "wait.task_failed")] == 0
    assert errs[("(multi)", "exit.3")] == 1
    db.close()


def test_usage_avg_ms_excludes_wait_outcomes(tmp_path: Path) -> None:
    """Long wait polls must not inflate channel-level avg_ms / max_ms (notice latency)."""
    from taskqueue.agent_stats import usage_by

    db = open_db(tmp_path / "q.db")
    # Fast CLI call + a 60s wait.timeout in the same channel bucket.
    db.execute(
        "INSERT INTO api_usage (bucket_ts, channel, client, operator, project, op, error_code, "
        "calls, total_ms, max_ms) VALUES "
        "('2099-01-01T00:00:00Z','cli','tq-cli','ada','p','tq list','',2,40,30),"
        "('2099-01-01T00:00:00Z','cli','tq-cli','ada','p','tq wait','wait.timeout',1,60000,60000)"
    )
    db.commit()
    rows = {r["channel"]: r for r in usage_by(db, by="channel", days=36500)}
    assert rows["cli"]["calls"] == 3
    assert rows["cli"]["errors"] == 0
    assert rows["cli"]["avg_ms"] == 20.0  # 40/2 — wait excluded
    assert rows["cli"]["max_ms"] == 30
    db.close()
