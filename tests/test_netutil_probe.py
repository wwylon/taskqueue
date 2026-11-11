"""F-228/F-229: loopback check is an IP check; probes never reach a blocked host (redirects included)."""

from __future__ import annotations

import http.server
import threading

import pytest

from tqdaemon.netutil import ProbeBlocked, check_probe_url, is_loopback_host, probe_get


@pytest.mark.parametrize(
    "host, ok",
    [
        ("localhost", True),
        ("127.0.0.1", True),
        ("127.5.6.7:8080", True),
        ("::1", True),
        ("[::1]", True),
        ("[::1]:11434", True),
        ("LOCALHOST", True),
        ("127.evil.example", False),  # F-228: a DNS name that starts with "127." is not loopback
        ("127.0.0.1.nip.io", False),
        ("10.0.0.1", False),
        ("example.com", False),
        ("", False),
    ],
)
def test_is_loopback_host(host, ok):
    assert is_loopback_host(host) is ok


def test_check_probe_url_rules():
    assert check_probe_url("http://127.0.0.1:11434/api/ps", allow_remote=False) is None
    assert "non-loopback" in check_probe_url("http://10.1.2.3/x", allow_remote=False)
    assert check_probe_url("http://10.1.2.3/x", allow_remote=True) is None
    assert "scheme" in check_probe_url("file:///etc/passwd", allow_remote=True)


class _Redirector(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path == "/ok":
            body = b'{"models": []}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(302)
        self.send_header("Location", "http://10.255.255.1/stolen" if self.path == "/away" else "/ok")
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Redirector)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_probe_follows_loopback_redirect(server):
    status, body = probe_get(f"{server}/home", allow_remote=False, timeout=5)
    assert status == 200 and b"models" in body


def test_probe_blocks_redirect_to_remote_before_connecting(server):
    with pytest.raises(ProbeBlocked, match="10.255.255.1"):
        probe_get(f"{server}/away", allow_remote=False, timeout=5)


def test_probe_blocks_remote_url_without_connecting():
    with pytest.raises(ProbeBlocked):
        probe_get("http://10.255.255.1/x", allow_remote=False, timeout=5)


def _no_connect(monkeypatch):
    calls = []

    def _open(req, timeout=None, allow_remote=False):
        calls.append(req.full_url)
        raise AssertionError(f"connected to {req.full_url}")

    monkeypatch.setattr("tqdaemon.netutil._open", _open)
    return calls


def test_remote_vram_probe_url_blocked_without_connecting(monkeypatch):
    """F-229: a custom vram_probe_url used to be fetched with no loopback check."""
    import io
    import json

    from tqdaemon import errors as tqerr
    from tqdaemon.scheduler import _check_services

    seen = []

    def _open(req, timeout=None, allow_remote=False):
        seen.append(req.full_url)
        if "10.255" in req.full_url:
            raise AssertionError("connected to remote VRAM probe")
        return io.BytesIO(b"{}")

    monkeypatch.setattr("tqdaemon.netutil._open", _open)
    svc = [
        {
            "name": "ollama",
            "health_url": "http://127.0.0.1:11434/",
            "min_vram_free_gib": 4,
            "vram_probe_url": "http://10.255.255.1/api/ps",
        }
    ]
    reason, code = _check_services(json.dumps(svc), system_info={"gpu_probe_stale": True})
    assert code == tqerr.BLOCK_SERVICE and "blocked" in reason
    assert seen == ["http://127.0.0.1:11434/"]


def test_remote_ollama_ps_not_fetched(monkeypatch):
    """F-229: the admit-time /api/ps fetch honours the SSRF policy."""
    from tqdaemon import scheduler

    scheduler._ollama_ps_cache.clear()
    calls = _no_connect(monkeypatch)
    assert scheduler._fetch_ollama_ps_models("http://10.255.255.1:11434/api/ps") is None
    assert calls == []
