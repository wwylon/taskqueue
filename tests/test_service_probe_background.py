"""F-232: daemon admit never runs a service probe on the scheduler thread."""

from __future__ import annotations

import threading
import time

from tqdaemon import errors as tqerr
from tqdaemon import scheduler
from tqdaemon.scheduler import SERVICE_PROBE_PENDING, check_services_cached, clear_service_probe_cache

SVC = '[{"health_url": "http://127.0.0.1:9/"}]'


def _wait(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_miss_returns_pending_and_probes_off_thread(monkeypatch):
    clear_service_probe_cache()
    caller = threading.get_ident()
    seen = []
    release = threading.Event()

    def slow(services_json, *, allow_remote=False, system_info=None):
        seen.append(threading.get_ident())
        release.wait(5)
        return (None, "")

    monkeypatch.setattr(scheduler, "_check_services", slow)
    woke = []
    monkeypatch.setattr("tqdaemon.sched_wake.wake_scheduler", lambda: woke.append(1))
    t0 = time.monotonic()
    assert check_services_cached(SVC, background=True) == (SERVICE_PROBE_PENDING, tqerr.BLOCK_SERVICE)
    assert check_services_cached(SVC, background=True)[0] == SERVICE_PROBE_PENDING  # deduped, still pending
    assert time.monotonic() - t0 < 1.0
    release.set()
    assert _wait(lambda: woke)
    assert seen and caller not in seen and len(seen) == 1
    assert check_services_cached(SVC, background=True) == (None, "")
    clear_service_probe_cache()


def test_expired_entry_served_stale_while_refreshing(monkeypatch):
    clear_service_probe_cache()
    results = iter([("down", tqerr.BLOCK_SERVICE), (None, "")])
    monkeypatch.setattr(scheduler, "_check_services", lambda *a, **k: next(results))
    monkeypatch.setattr("tqdaemon.sched_wake.wake_scheduler", lambda: None)
    assert check_services_cached(SVC)[0] == "down"  # sync fill
    key = next(iter(scheduler._svc_probe_cache))
    ts, val = scheduler._svc_probe_cache[key]
    scheduler._svc_probe_cache[key] = (ts - scheduler._SVC_PROBE_TTL_SEC - 1, val)
    assert check_services_cached(SVC, background=True)[0] == "down"  # stale, refresh started
    assert _wait(lambda: check_services_cached(SVC, background=True) == (None, ""))
    clear_service_probe_cache()


def test_ollama_ps_background_never_blocks(monkeypatch):
    scheduler._ollama_ps_cache.clear()
    gate = threading.Event()

    def slow(*a, **k):
        gate.wait(5)
        return 200, b'{"models": [{"name": "m"}]}'

    monkeypatch.setattr("tqdaemon.netutil.probe_get", slow)
    url = "http://127.0.0.1:11434/api/ps"
    t0 = time.monotonic()
    assert scheduler._fetch_ollama_ps_models(url, background=True) is None
    assert time.monotonic() - t0 < 1.0
    gate.set()
    assert _wait(lambda: scheduler._fetch_ollama_ps_models(url, background=True) == [{"name": "m"}])
    scheduler._ollama_ps_cache.clear()


def test_no_health_url_is_healthy_without_probe_or_pending(monkeypatch):
    """F-232 follow-up: the default "[]" must not wait on a probe that has nothing to check."""
    clear_service_probe_cache()
    monkeypatch.setattr(scheduler, "_check_services", lambda *a, **k: (_ for _ in ()).throw(AssertionError("probed")))
    for raw in ("[]", "", None, '[{"name": "x"}]'):
        assert check_services_cached(raw, background=True) == (None, "")
    assert not scheduler._svc_probe_cache and not scheduler._svc_inflight
