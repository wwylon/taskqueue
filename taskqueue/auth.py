"""Bearer / X-TQ-Token auth helpers."""

from __future__ import annotations

import hmac
from typing import Any


def token_required(cfg: dict[str, Any]) -> bool:
    tok = cfg.get("auth_token") or ""
    return bool(str(tok).strip())


def configured_token(cfg: dict[str, Any]) -> str:
    return str(cfg.get("auth_token") or "").strip()


def extract_request_token(headers: dict[str, str], query_token: str | None = None) -> str | None:
    """Pull token from Authorization Bearer, X-TQ-Token, or query."""
    auth = headers.get("Authorization") or headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    xt = headers.get("X-TQ-Token") or headers.get("x-tq-token")
    if xt:
        return xt.strip()
    if query_token:
        return query_token.strip()
    return None


def client_auth_headers(
    token: str | None = None,
    *,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Outbound client headers: Bearer + X-TQ-Token when a token is set."""
    h: dict[str, str] = {"Content-Type": "application/json", "Accept": "application/json"}
    if extra:
        h.update(extra)
    tok = (token or "").strip()
    if tok:
        h["Authorization"] = f"Bearer {tok}"
        h["X-TQ-Token"] = tok
    return h


def check_token(cfg: dict[str, Any], provided: str | None) -> bool:
    """True if auth not required or provided matches."""
    expected = configured_token(cfg)
    if not expected:
        return True
    if not provided:
        return False
    return hmac.compare_digest(provided, expected)


def finalize_token_matches(stored: Any, given: Any) -> bool:
    """Constant-time check of a per-claim finalize token; a missing token on either side never matches."""
    if not stored or not given:
        return False
    return hmac.compare_digest(str(stored).encode(), str(given).encode())
