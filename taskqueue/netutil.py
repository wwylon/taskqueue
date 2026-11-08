"""Small network helpers shared by the web layer, scheduler probes and notify."""

from __future__ import annotations

import ipaddress
import urllib.error
import urllib.request
from urllib.parse import urlparse


def is_loopback_host(host: str) -> bool:
    """True for ``localhost``, ``::1`` and 127.0.0.0/8 (an optional ``:port`` suffix is ignored).

    F-228: an IP check, not a prefix match — ``127.evil.example`` is a DNS name that can
    resolve anywhere, so it is not loopback.
    """
    h = (host or "").strip().lower()
    if h.startswith("["):  # [::1] or [::1]:port
        h = h[1:].split("]", 1)[0]
    elif h.count(":") == 1:  # host:port (a bare IPv6 address has several colons)
        h = h.split(":", 1)[0]
    if h == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    return bool((mapped or ip).is_loopback)


class ProbeBlocked(urllib.error.URLError):
    """A probe URL (or a redirect hop) points at a host the policy does not allow."""


def check_probe_url(url: str, *, allow_remote: bool) -> str | None:
    """Why *url* may not be fetched, or None when it may."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return f"unparseable URL {url!r}"
    if parsed.scheme not in ("http", "https"):
        return f"unsupported scheme {parsed.scheme!r} in {url!r}"
    host = (parsed.hostname or "").lower()
    if not host:
        return f"URL {url!r} has no host"
    if not allow_remote and not is_loopback_host(host):
        return f"non-loopback host {host!r} blocked (SSRF guard); set notify_allow_remote=1 to allow"
    return None


def loopback_opener(*, allow_remote: bool = False) -> urllib.request.OpenerDirector:
    """urllib opener that re-validates every redirect hop *before* following it."""

    class _ValidatingRedirectHandler(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            why = check_probe_url(newurl, allow_remote=allow_remote)
            if why:
                raise ProbeBlocked(f"redirect blocked: {why}")
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    return urllib.request.build_opener(_ValidatingRedirectHandler())


def _open(req: urllib.request.Request, *, timeout: float, allow_remote: bool):
    """Open *req* with redirect validation (test seam for probe stubs)."""
    return loopback_opener(allow_remote=allow_remote).open(req, timeout=timeout)


def probe_get(url: str, *, allow_remote: bool, timeout: float) -> tuple[int, bytes]:
    """GET a health / VRAM probe URL under the SSRF policy (F-229).

    The URL and every redirect hop are checked before any connection is made to them.
    Returns ``(status, body)``; raises ``ProbeBlocked`` for a disallowed host and the
    usual ``urllib`` errors otherwise (HTTP >= 400 is returned, not raised).
    """
    why = check_probe_url(url, allow_remote=allow_remote)
    if why:
        raise ProbeBlocked(why)
    req = urllib.request.Request(url, method="GET")
    try:
        with _open(req, timeout=timeout, allow_remote=allow_remote) as resp:
            return int(getattr(resp, "status", 200) or 200), resp.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), b""
