"""Per-token project scoping: default-deny allowlist, leak matrix over every registered route."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from taskqueue.db import open_db, set_config
from taskqueue.operator_features import create_token, list_tokens
from taskqueue.web import make_handler
from taskqueue.web.api_docs import _API_REGISTRY, _register_api_docs
from tests.test_web_routes import _make_request

_ALLOWED_GET = ("/api/queue", "/api/history", "/api/audit")


@pytest.fixture()
def env(tmp_path: Path):
    db = open_db(tmp_path / "q.db")
    set_config(db, "auth_token", "legacy-secret-token")
    handler = make_handler(db, {}, {}, lambda: {}, tmp_path / "q.db", "127.0.0.1")
    return handler, db


def _call(handler, method: str, path: str, token: str | None):
    hdr = {"Authorization": f"Bearer {token}"} if token else {}
    # no client_address on the fake handler => treated as a remote caller (auth enforced)
    code, body = _make_request(handler, method, path, b"{}" if method != "GET" else b"", hdr)
    return code, body


def test_projects_stored_and_listed(env):
    _, db = env
    t = create_token(db, "ci", "read", ["proj", "proj", " other "])
    assert t["projects"] == ["proj", "other"]
    assert list_tokens(db)[0]["projects"] == ["proj", "other"]
    assert create_token(db, "all", "read")["projects"] == []
    with pytest.raises(ValueError):
        create_token(db, "bad", "admin", ["proj"])


def test_scoped_token_allowed_reads(env):
    handler, db = env
    tok = create_token(db, "ci", "read", ["proj"])["token"]
    for path in _ALLOWED_GET:
        assert _call(handler, "GET", f"{path}?project=proj", tok)[0] == 200, path
        assert _call(handler, "GET", f"{path}?project=other", tok)[0] == 403, path
        assert _call(handler, "GET", path, tok)[0] == 403, path
        assert _call(handler, "GET", f"{path}?project=proj&project=other", tok)[0] == 403, path


def test_unscoped_token_unaffected(env):
    handler, db = env
    tok = create_token(db, "all", "read")["token"]
    assert _call(handler, "GET", "/api/queue", tok)[0] == 200
    assert _call(handler, "GET", "/api/status", tok)[0] == 200


def test_leak_matrix_every_registered_route_denied(env):
    handler, db = env
    if not _API_REGISTRY:
        _register_api_docs()
    tok = create_token(db, "ci", "write", ["proj"])["token"]
    checked = 0
    for e in list(_API_REGISTRY):
        method, raw = e["method"], e["path"]
        path = re.sub(r"<[^>]+>", "1", raw)
        if method == "GET" and path in _ALLOWED_GET:
            continue
        if method == "POST" and path == "/api/sse-ticket":
            continue  # intentionally allowed: the ticket inherits the caller's own scope
        # token-authenticated callers may not use worker paths at all; still must not succeed
        code, body = _call(handler, method, path + "?project=proj", tok)
        assert code in (401, 403), (method, raw, code, body[:200])
        checked += 1
    assert checked > 20


def test_unparseable_stored_list_fails_closed(env):
    handler, db = env
    tok = create_token(db, "ci", "read", ["proj"])["token"]
    db.execute("UPDATE api_tokens SET projects = 'not json'")
    db.commit()
    assert _call(handler, "GET", "/api/queue?project=proj", tok)[0] == 403
