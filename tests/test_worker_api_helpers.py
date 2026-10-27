"""worker_api claim helpers: service pre-check, peer nodes, response builder."""
from types import SimpleNamespace

from taskqueue import worker_api
from taskqueue.config import PROTOCOL_VERSION
from taskqueue.db import open_db


def test_svc_key_is_order_stable_and_tolerates_garbage():
    assert worker_api._svc_key('[{"b":1,"a":2}]') == worker_api._svc_key('[{"a":2,"b":1}]')
    assert worker_api._svc_key("") == "[]"


def test_precheck_services_probes_each_distinct_requirement_once(tmp_path, monkeypatch):
    import taskqueue.scheduler as scheduler

    conn = open_db(tmp_path / "q.db")
    rows = [{"requires_services": '[{"health_url":"http://x"}]'}] * 3 + [{"requires_services": None}]

    class Rdb:
        def execute(self, sql):
            return SimpleNamespace(fetchall=lambda: rows)

    calls = []
    monkeypatch.setattr(
        scheduler, "check_services_cached",
        lambda sv, allow_remote=False, system_info=None: calls.append(sv) or ("down", ""),
    )
    worker_api._QUEUED_SVC_CACHE.pop("k", None)
    out = worker_api._precheck_services(Rdb(), "k", {"notify_allow_remote": 1})
    worker_api._QUEUED_SVC_CACHE.pop("k", None)
    assert len(calls) == 2 and set(out.values()) == {"down"}
    conn.close()


def test_claim_response_shape_and_override_dropped():
    resp = worker_api._claim_response(
        {"project": "p", "task_id": "t", "args": '["--a"]', "args_override": None,
         "resources": "{}", "requires_paths": "[]"},
        lambda: {}, "p", 7, "/l.log", True, 60, "tok",
    )
    assert resp["run_id"] == 7 and resp["use_resume"] is True
    assert resp["finalize_token"] == "tok" and resp["protocol"] == PROTOCOL_VERSION
    assert resp["task"]["args"] == ["--a"] and "args_override" not in resp["task"]
