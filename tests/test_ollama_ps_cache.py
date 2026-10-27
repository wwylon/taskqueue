import io
import json

from taskqueue import scheduler


def test_ps_fetch_cached(monkeypatch):
    scheduler._ollama_ps_cache.clear()
    calls = []

    def fake(req, timeout=0):
        calls.append(1)
        return io.BytesIO(json.dumps({"models": [{"name": "m"}]}).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake)
    a = scheduler._fetch_ollama_ps_models("http://x/api/ps")
    b = scheduler._fetch_ollama_ps_models("http://x/api/ps")
    assert a == b == [{"name": "m"}]
    assert len(calls) == 1


def test_ps_failure_cached(monkeypatch):
    scheduler._ollama_ps_cache.clear()
    calls = []

    def boom(req, timeout=0):
        calls.append(1)
        raise OSError("down")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    assert scheduler._fetch_ollama_ps_models("http://y/api/ps") is None
    assert scheduler._fetch_ollama_ps_models("http://y/api/ps") is None
    assert len(calls) == 1
