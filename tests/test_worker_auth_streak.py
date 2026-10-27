from __future__ import annotations

import io
import urllib.error

import pytest

from taskqueue import worker


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http(code):
    return urllib.error.HTTPError("http://m", code, "x", {}, io.BytesIO(b""))


def test_success_resets_auth_failure_streak(monkeypatch):
    worker._post._auth_fail_streak = 0
    seq = [_http(403), _http(403), _Resp(b'{"ok": true}'), _http(403), _http(403)]

    def fake_open(req, timeout=0):
        item = seq.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(worker.urllib.request, "urlopen", fake_open)
    for _ in range(5):
        worker._post("http://m/x", {})  # must never exit: failures are not consecutive
    assert worker._post._auth_fail_streak == 2


def test_three_consecutive_auth_failures_exit(monkeypatch):
    worker._post._auth_fail_streak = 0

    def fake_open(req, timeout=0):
        raise _http(401)

    monkeypatch.setattr(worker.urllib.request, "urlopen", fake_open)
    with pytest.raises(SystemExit):
        for _ in range(3):
            worker._post("http://m/x", {})
