"""Config audit records before/after values and can be rolled back."""

from __future__ import annotations

import json

from taskqueue.db import get_config
from taskqueue.operator_features import list_audit
from tests.helpers.master_worker import http_request, make_handler_env


def test_config_change_records_diff_and_rolls_back(tmp_path):
    handler, db, _home = make_handler_env(tmp_path)
    old = get_config(db)["scheduler_interval_sec"]
    new = old + 7
    assert http_request(handler, "POST", "/api/config", {"scheduler_interval_sec": new})[0] == 200
    ev = [e for e in list_audit(db) if e["action"] == "config"][0]
    assert json.loads(ev["detail"])["changes"]["scheduler_interval_sec"] == {"old": old, "new": new}
    status, body = http_request(handler, "POST", f"/api/audit/{ev['id']}/rollback", {})
    assert status == 200 and body["restored"] == ["scheduler_interval_sec"]
    assert get_config(db)["scheduler_interval_sec"] == old
    assert any(e["action"] == "config_rollback" for e in list_audit(db))


def test_rollback_rejections(tmp_path):
    handler, db, _home = make_handler_env(tmp_path)
    assert http_request(handler, "POST", "/api/audit/9999/rollback", {})[0] == 404
    assert http_request(handler, "POST", "/api/audit/abc/rollback", {})[0] == 400
    http_request(handler, "POST", "/api/health/ack", {})
    ev = list_audit(db)[0]
    assert http_request(handler, "POST", f"/api/audit/{ev['id']}/rollback", {})[0] == 409


def test_secret_not_recorded_or_restored(tmp_path):
    handler, db, _home = make_handler_env(tmp_path)
    http_request(handler, "POST", "/api/config", {"auth_token": "supersecretvalue1"})
    ev = [e for e in list_audit(db) if e["action"] == "config"][0]
    assert "supersecretvalue1" not in ev["detail"]
    status, body = http_request(handler, "POST", f"/api/audit/{ev['id']}/rollback", {})
    assert body["restored"] == [] and body["skipped"]["auth_token"] == "secret"
