import io
import json

from tqdaemon import scheduler


def test_ps_fetch_cached(monkeypatch):
    scheduler._ollama_ps_cache.clear()
    calls = []

    def fake(req, timeout=0):
        calls.append(1)
        return io.BytesIO(json.dumps({"models": [{"name": "m"}]}).encode())

    monkeypatch.setattr(
        "tqdaemon.netutil._open", lambda req, timeout=None, allow_remote=False: fake(req, timeout=timeout)
    )
    a = scheduler._fetch_ollama_ps_models("http://127.0.0.1:11434/api/ps")
    b = scheduler._fetch_ollama_ps_models("http://127.0.0.1:11434/api/ps")
    assert a == b == [{"name": "m"}]
    assert len(calls) == 1


def test_ps_failure_cached(monkeypatch):
    scheduler._ollama_ps_cache.clear()
    calls = []

    def boom(req, timeout=0):
        calls.append(1)
        raise OSError("down")

    monkeypatch.setattr(
        "tqdaemon.netutil._open", lambda req, timeout=None, allow_remote=False: boom(req, timeout=timeout)
    )
    assert scheduler._fetch_ollama_ps_models("http://127.0.0.2:11434/api/ps") is None
    assert scheduler._fetch_ollama_ps_models("http://127.0.0.2:11434/api/ps") is None
    assert len(calls) == 1
