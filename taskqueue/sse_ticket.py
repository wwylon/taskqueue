"""Short-lived tickets so EventSource URLs need not carry the long-lived API token.

``EventSource`` cannot set headers, so the UI used ``?tq_token=<token>`` — which lands in
access logs, browser history and Referer headers. Instead the UI POSTs ``/api/sse-ticket``
(normal header auth) and opens the stream with ``?tq_ticket=<ticket>``. A ticket is a random
string valid for ``TICKET_TTL_SEC``; it stands in for the issuer's token on GET streaming
paths only (``/api/events``, ``/api/logs/<id>/stream``), so its scope and project limits are
unchanged and it grants nothing else. Tickets live in memory and die with the daemon.
"""

from __future__ import annotations

import re
import secrets
import threading
import time

TICKET_TTL_SEC = 120
_MAX_TICKETS = 2000
_STREAM_PATH = re.compile(r"^/api/(events|logs/\d+/stream)$")

_lock = threading.Lock()
_tickets: dict[str, tuple[str, float]] = {}


def is_stream_path(path: str) -> bool:
    return bool(_STREAM_PATH.match(path.rstrip("/") or "/"))


def issue_ticket(token: str) -> str:
    now = time.monotonic()
    ticket = secrets.token_urlsafe(24)
    with _lock:
        for k in [k for k, (_, exp) in _tickets.items() if exp <= now]:
            del _tickets[k]
        if len(_tickets) >= _MAX_TICKETS:
            del _tickets[min(_tickets, key=lambda k: _tickets[k][1])]
        _tickets[ticket] = (token, now + TICKET_TTL_SEC)
    return ticket


def redeem_ticket(ticket: str) -> str | None:
    """Return the issuer's token while *ticket* is unexpired, else None."""
    with _lock:
        hit = _tickets.get(ticket)
        if hit is None:
            return None
        if hit[1] <= time.monotonic():
            del _tickets[ticket]
            return None
        return hit[0]
