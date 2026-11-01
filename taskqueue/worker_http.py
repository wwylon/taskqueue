"""HTTP helpers for the remote worker (extracted from worker.py, F-208).

Owns the process-wide auth token used on master calls, plus POST/GET wrappers
with auth-fail streak handling.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

_AUTH_TOKEN: str = ""


def set_auth_token(token: str | None) -> None:
    global _AUTH_TOKEN
    _AUTH_TOKEN = (token or os.environ.get("TQ_AUTH_TOKEN") or "").strip()


def auth_token() -> str:
    return _AUTH_TOKEN


def _auth_headers(extra: dict | None = None) -> dict:
    from taskqueue.auth import client_auth_headers

    return client_auth_headers(_AUTH_TOKEN, extra=extra)


def post_json(url: str, data: dict, *, timeout: float = 30) -> dict | None:
    """POST JSON to master; exit after 3 consecutive 401/403 responses.

    Auth-fail streak is stored on the function (``post_json._auth_fail_streak``) so
    tests and ``worker._post`` aliases can reset/inspect it.
    """
    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode("utf-8"),
        headers=_auth_headers({"Content-Type": "application/json"}),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            post_json._auth_fail_streak = 0  # type: ignore[attr-defined]
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            # Count consecutive auth failures to distinguish transient proxy 403s from
            # a permanently invalid token.  Only exit after repeated failures.
            streak = getattr(post_json, "_auth_fail_streak", 0) + 1
            post_json._auth_fail_streak = streak  # type: ignore[attr-defined]
            if streak >= 3:
                print(
                    f"[worker] FATAL: authentication rejected by master (HTTP {e.code}) "
                    f"{streak} times in a row. Check TQ_AUTH_TOKEN. Exiting.",
                    file=sys.stderr,
                )
                sys.exit(1)
            print(
                f"[worker] auth error HTTP {e.code} (streak {streak}/3); will retry next cycle",
                file=sys.stderr,
            )
            return None
        post_json._auth_fail_streak = 0  # type: ignore[attr-defined]
        print(f"[worker] API error (POST {url}): {e}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[worker] API error (POST {url}): {e}", file=sys.stderr)
        return None


post_json._auth_fail_streak = 0  # type: ignore[attr-defined]


def get_json(url: str, timeout: float = 10) -> dict | None:
    try:
        req = urllib.request.Request(url, headers=_auth_headers(), method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[worker] API error (GET {url}): {e}", file=sys.stderr)
        return None
