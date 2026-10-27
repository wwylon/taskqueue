"""Small network helpers shared by the web layer, scheduler probes and notify."""
from __future__ import annotations


def is_loopback_host(host: str) -> bool:
    """True for localhost, ::1 and 127.0.0.0/8 (an optional ``:port`` suffix is ignored)."""
    h = (host or "").lower().strip("[]")
    # Strip a port suffix for IPv4/hostname (but not for bare IPv6 like ::1).
    if ":" in h and h.count(":") == 1:
        h = h.split(":")[0]
    return h in ("localhost", "::1") or h.startswith("127.")
