"""Short-lived SSE tickets keep the API token out of EventSource URLs."""

from __future__ import annotations

from taskqueue import sse_ticket
from taskqueue.operator_features import project_scope_allows, scope_allows
from tests.helpers.master_worker import http_request, make_handler_env


def test_ticket_roundtrip_and_expiry(monkeypatch):
    t = sse_ticket.issue_ticket("tok")
    assert sse_ticket.redeem_ticket(t) == "tok"
    assert sse_ticket.redeem_ticket("nope") is None
    real = sse_ticket.time.monotonic
    monkeypatch.setattr(sse_ticket.time, "monotonic", lambda: real() + sse_ticket.TICKET_TTL_SEC + 1)
    assert sse_ticket.redeem_ticket(t) is None


def test_stream_paths_only():
    assert sse_ticket.is_stream_path("/api/events")
    assert sse_ticket.is_stream_path("/api/logs/12/stream")
    assert not sse_ticket.is_stream_path("/api/tasks")
    assert not sse_ticket.is_stream_path("/api/config")


def test_scopes_allow_ticket_post():
    assert scope_allows("read", "POST", "/api/sse-ticket")
    assert scope_allows("write", "POST", "/api/sse-ticket")
    assert not scope_allows("read", "POST", "/api/stop/p/t")
    assert project_scope_allows(["p"], "POST", "/api/sse-ticket", {})


def test_endpoint_no_token_returns_null_ticket(tmp_path):
    handler, _db, _home = make_handler_env(tmp_path)
    status, body = http_request(handler, "POST", "/api/sse-ticket", {})
    assert status == 200 and body["ticket"] is None


def test_endpoint_issues_redeemable_ticket(tmp_path):
    handler, _db, _home = make_handler_env(tmp_path)
    status, body = http_request(handler, "POST", "/api/sse-ticket", {}, extra_headers={"Authorization": "Bearer abc"})
    assert status == 200 and sse_ticket.redeem_ticket(body["ticket"]) == "abc"
