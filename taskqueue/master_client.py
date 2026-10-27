"""HTTP client for the master daemon (gpt.md P6 / R-010).

CLI and MCP mutations should go through this API so the daemon is the only
ordinary SQLite writer. Direct DB access remains for offline repair when the
daemon is unreachable.

Master URL: ``TQ_MASTER`` (e.g. ``http://127.0.0.1:8766``) or
``http://127.0.0.1:<web_port>`` from this home's config.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def master_url(home: Path) -> str:
    env = (os.environ.get("TQ_MASTER") or "").strip().rstrip("/")
    if env:
        return env
    from taskqueue._cli_helpers import _ui_url

    return _ui_url(home)


_IDENTITY: dict[str, str] = {"via": "cli", "client": ""}


def set_identity(via: str, client: str = "") -> None:
    """Declare which interface this process is (``cli`` default; MCP sets ``mcp``)."""
    _IDENTITY["via"] = via
    _IDENTITY["client"] = client


def identity_headers() -> dict[str, str]:
    """Provenance headers (analytics only; self-reported, never used for auth)."""
    from taskqueue import __version__

    hdrs = {
        "X-TQ-Via": _IDENTITY["via"],
        "X-TQ-Client": _IDENTITY["client"] or f"tq-{_IDENTITY['via']}/{__version__}",
    }
    op = (os.environ.get("TQ_OPERATOR") or "").strip()
    if op:
        hdrs["X-TQ-Operator"] = op
    return hdrs


def _auth_headers(home: Path) -> dict[str, str]:
    from taskqueue.auth import client_auth_headers

    token = (os.environ.get("TQ_AUTH_TOKEN") or "").strip()
    if not token:
        from taskqueue.db import read_config

        token = str(read_config(home).get("auth_token") or "").strip()
    return client_auth_headers(token)


def post_json(base_url: str, path: str, body: dict[str, Any] | None = None, *,
              token: str = "", timeout: float = 10.0) -> dict[str, Any]:
    """POST JSON to an explicit master URL (worker admin commands). Raises on network errors.

    Unlike ``via_master`` this does not look up a local daemon: the caller names the master.
    HTTP error responses with a JSON body are returned as that body.
    """
    import urllib.error
    import urllib.request

    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-TQ-Token"] = token
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(f"{base_url.rstrip('/')}{path}", data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode() or "{}")
        except ValueError:
            return {"ok": False, "error": f"HTTP {exc.code}"}


def api_request(
    home: Path,
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    timeout: float = 8.0,
) -> dict[str, Any] | None:
    """POST/GET JSON. Returns parsed dict, or None if the daemon is unreachable."""
    url = master_url(home) + (path if path.startswith("/") else "/" + path)
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method.upper(),
                                 headers={**identity_headers(), **_auth_headers(home)})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            if not raw.strip():
                return {"ok": True}
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            parsed = {"error": raw or str(e)}
        if isinstance(parsed, dict):
            parsed.setdefault("ok", False)
            parsed.setdefault("http_status", e.code)
            return parsed
        return {"ok": False, "error": str(e), "http_status": e.code}
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return None


# Full-portfolio YAML sync can take minutes; keep this above typical host loads.
CLI_SYNC_TIMEOUT_SEC = 600.0

# Probe timeout used before any long-timeout call to avoid blocking on a stale PID.
_PROBE_TIMEOUT_SEC = 2.0
_PROBE_THRESHOLD_SEC = 10.0  # only probe when the caller's timeout exceeds this


def via_master(
    home: Path,
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    timeout: float = 8.0,
) -> dict[str, Any] | None:
    """Call the master. None means offline — caller may fall back to SQLite.

    Does not contact a random localhost port: unless ``TQ_MASTER`` is set, the
    daemon pid for this home must be alive.

    ``timeout`` is the HTTP wait (seconds). Use ``CLI_SYNC_TIMEOUT_SEC`` for
    ``/api/cli/sync`` — the default 8s is too short for large operator queues.

    When ``timeout`` exceeds ``_PROBE_THRESHOLD_SEC`` a fast /api/status probe
    is sent first (2 s).  A stale PID after a reboot would otherwise hold the
    caller for the full timeout before falling through to offline mode.
    """
    local_daemon = False
    if not (os.environ.get("TQ_MASTER") or "").strip():
        from taskqueue._cli_helpers import _daemon_running

        if _daemon_running(home) is None:
            return None
        local_daemon = True

    if timeout > _PROBE_THRESHOLD_SEC and path != "/api/status":
        if api_request(home, "GET", "/api/status", timeout=_PROBE_TIMEOUT_SEC) is None:
            return None

    resp = api_request(home, method, path, body=body, timeout=timeout)
    if resp is None and local_daemon:
        # The pid is alive, so the master owns the DB: a timeout must not make
        # callers fall back to writing SQLite directly (split brain).
        return {
            "ok": False,
            "error": "master is running but did not answer (timeout/connection error); "
            "not falling back to direct SQLite writes - retry shortly",
            "unreachable": True,
        }
    return resp
