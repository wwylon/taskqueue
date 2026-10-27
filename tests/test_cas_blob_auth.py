"""GET /api/data/blob/<sha> authorizes through the shared run-token check."""
from __future__ import annotations

from tests.helpers.master_worker import http_request, make_handler_env

SHA = "a" * 64


def test_non_numeric_run_id_is_400(tmp_path):
    handler, _db, _home = make_handler_env(tmp_path)
    status, _ = http_request(
        handler, "GET", f"/api/data/blob/{SHA}?run_id=abc&finalize_token=x"
    )
    assert status == 400


def test_missing_credentials_is_401(tmp_path):
    handler, _db, _home = make_handler_env(tmp_path)
    status, _ = http_request(handler, "GET", f"/api/data/blob/{SHA}")
    assert status == 401
    status, _ = http_request(handler, "GET", f"/api/data/blob/{SHA}?run_id=1")
    assert status == 401


def test_unknown_run_is_404(tmp_path):
    handler, _db, _home = make_handler_env(tmp_path)
    status, _ = http_request(
        handler, "GET", f"/api/data/blob/{SHA}?run_id=999&finalize_token=x"
    )
    assert status == 404
