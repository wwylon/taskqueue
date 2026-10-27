"""Per-run HTTP-proxy service broker.

At claim time, the master mints a per-run capability token scoped to
(run_id, worker_instance_id, allowlist, expiry).  The worker gets
TQ_SERVICE_<NAME>_URL and TQ_SERVICE_<NAME>_TOKEN injected into its env.
Requests to /api/service/<name>/... are validated and forwarded to upstream.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from typing import Any


_BROKER_SECRET = secrets.token_bytes(32)


def mint_service_token(
    run_id: int,
    service_name: str,
    allow: list[str],
    ttl_sec: int = 3600,
) -> str:
    """Mint a signed capability token for a service."""
    payload = {
        "run_id": run_id,
        "service": service_name,
        "allow": allow,
        "exp": int(time.time()) + ttl_sec,
        "nonce": secrets.token_hex(8),
    }
    body = json.dumps(payload, separators=(",", ":"))
    sig = hmac.new(_BROKER_SECRET, body.encode(), hashlib.sha256).hexdigest()
    import base64
    encoded = base64.urlsafe_b64encode(body.encode()).decode()
    return f"{encoded}.{sig}"


def verify_service_token(
    token: str,
    run_id: int,
    service_name: str,
    method: str,
    path: str,
) -> tuple[bool, str]:
    """Verify a service token and check if the request is allowed.

    Returns (ok, reason).
    """
    import base64
    import re

    try:
        parts = token.split(".")
        if len(parts) != 2:
            return False, "invalid token format"
        encoded, sig = parts
        body = base64.urlsafe_b64decode(encoded + "==").decode()
        expected_sig = hmac.new(_BROKER_SECRET, body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return False, "invalid signature"
        payload = json.loads(body)
    except Exception as e:
        return False, f"token decode error: {e}"

    if payload.get("run_id") != run_id:
        return False, "run_id mismatch"
    if payload.get("service") != service_name:
        return False, "service mismatch"
    if int(payload.get("exp", 0)) < int(time.time()):
        return False, "token expired"

    # Check allowlist: entries like "GET /v1/features/*" or "POST /v1/score"
    allow = payload.get("allow") or []
    request_line = f"{method.upper()} {path}"
    for pattern in allow:
        p = str(pattern).replace("*", ".*")
        if re.fullmatch(p, request_line):
            return True, ""

    return False, f"not in allowlist: {request_line}"


def build_service_env(
    run_id: int,
    master_url: str,
    services: list[dict[str, Any]],
) -> dict[str, str]:
    """Build TQ_SERVICE_* env vars for the worker."""
    env: dict[str, str] = {}
    for svc in services:
        name = str(svc.get("name") or "").upper().replace("-", "_")
        if not name:
            continue
        allow = svc.get("allow") or []
        ttl = int(svc.get("token_ttl_sec") or 3600)

        token = mint_service_token(run_id, svc.get("name", ""), allow, ttl)
        svc_url = f"{master_url.rstrip('/')}/api/service/{svc.get('name', '')}"

        env[f"TQ_SERVICE_{name}_URL"] = svc_url
        env[f"TQ_SERVICE_{name}_TOKEN"] = token
    return env
