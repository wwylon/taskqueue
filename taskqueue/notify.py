"""Opt-in task notifications (webhook + home notify.jsonl + Windows toast).

Config keys (set via ``tq config set <key> <value>``):
  notify_url            Global fallback webhook URL (used when task has no per-task notify_url).
  notify_allow_remote   Set to 1 to allow non-loopback webhook hosts.
  toast_notifications   Set to false/0 to suppress task-end/start Windows toasts
                        (DB default is off). System-alert Windows popups are gated
                        separately by each alert's ``windows_toast`` flag.

Windows OS toasts require the optional ``winotify`` package::

    pip install winotify
    # or: pip install 'taskqueue[toast]' / uv sync --extra toast

Without ``winotify``, toast calls no-op after a one-time WARNING log; system alerts
still append to ``notify.jsonl`` for the web UI bell.
"""

from __future__ import annotations

import json
import logging
import queue
import os
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

from taskqueue.netutil import is_loopback_host
from urllib.request import Request
import urllib.request as _urlrequest

log = logging.getLogger("taskqueue.notify")


def redact_notify_url(url: str) -> str:
    """F-120: strip userinfo and query/fragment before API / jsonl exposure."""
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        p = urlparse(raw)
    except Exception:
        return "<redacted>"
    host = p.hostname or ""
    if p.port:
        host = f"{host}:{p.port}"
    if p.username is not None or p.password is not None:
        host = f"***@{host}"
    return urlunparse((p.scheme or "", host, p.path or "", "", "", ""))


NOTIFY_LOG_NAME = "notify.jsonl"
NOTIFY_FAILED_LOG_NAME = "notify_failed.jsonl"
from taskqueue.statuses import TERMINAL_STATUSES  # noqa: E402  # one definition, shared with tq wait
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})  # kept for importers; checks use is_loopback_host


def _make_no_redirect_opener(*, allow_remote: bool = False):
    """Return a urllib opener that re-validates the host on every redirect."""

    class _ValidatingRedirectHandler(_urlrequest.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            host = (urlparse(newurl).hostname or "").lower()
            if not allow_remote and not is_loopback_host(host):
                from urllib.error import URLError
                raise URLError(
                    f"redirect to non-loopback host {host!r} blocked "
                    "(set notify_allow_remote=1 to permit)"
                )
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    return _urlrequest.build_opener(_ValidatingRedirectHandler())


_POST_TIMEOUT_SEC = 4.0
_POST_RETRIES = 3
_POST_RETRY_DELAYS = (1.0, 4.0, 9.0)
_winotify_missing_logged = False
_toast_fail_logged = False

# Bounded queue + small thread pool for webhook POSTs.
# Replaces the per-notification daemon threads that caused unbounded thread growth
# under burst completions. Dropped items (queue full) are logged as warnings.
# A pool of _WEBHOOK_WORKERS threads prevents a slow endpoint (3 retries × backoff)
# from blocking all other notifications.
_WEBHOOK_QUEUE: queue.Queue = queue.Queue(maxsize=256)
_WEBHOOK_WORKERS = 3
_webhook_workers_started = False
_webhook_worker_lock = threading.Lock()


def _webhook_worker() -> None:
    while True:
        item = _WEBHOOK_QUEUE.get()
        if item is None:
            break
        url, payload, home, allow_remote = item
        try:
            _post_webhook(url, payload, home, allow_remote=allow_remote)
        except Exception:
            pass
        finally:
            _WEBHOOK_QUEUE.task_done()


def _ensure_webhook_worker() -> None:
    global _webhook_workers_started
    if _webhook_workers_started:
        return
    with _webhook_worker_lock:
        if not _webhook_workers_started:
            for i in range(_WEBHOOK_WORKERS):
                t = threading.Thread(
                    target=_webhook_worker,
                    name=f"tq-notify-worker-{i}",
                    daemon=True,
                )
                t.start()
            _webhook_workers_started = True


def notify_log_path(home: Path) -> Path:
    return Path(home) / NOTIFY_LOG_NAME


def notify_failed_log_path(home: Path) -> Path:
    return Path(home) / NOTIFY_FAILED_LOG_NAME


def validate_notify_url(url: str, *, allow_remote: bool = False) -> str:
    """Normalize and validate notify_url. Raises ValueError on bad input."""
    raw = (url or "").strip()
    if not raw:
        raise ValueError("notify_url is empty")
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"notify_url must be http(s), got scheme={parsed.scheme!r}")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError("notify_url missing host")
    if not allow_remote and not is_loopback_host(host):
        raise ValueError(
            f"notify_url host {host!r} is not loopback; "
            "set notify_allow_remote=1 to allow remote webhooks"
        )
    return raw


from taskqueue._time import _now as _now_iso  # noqa: E402


def build_notify_payload(
    *,
    project: str,
    task_id: str,
    status: str,
    run_id: int | None,
    exit_code: int | None,
    last_error_code: str | None,
    started_by: str | None,
    notify_url: str,
    ts: str | None = None,
    # enriched fields (all optional for back-compat)
    description: str | None = None,
    suite: str | None = None,
    elapsed_sec: float | None = None,
    peak_ram_gib: float | None = None,
    progress_pct: float | None = None,
    block_reason: str | None = None,
    stop_reason: str | None = None,
    submitted_by: str | None = None,
    submitted_via: str | None = None,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ts": ts or _now_iso(),
        "project": project,
        "task_id": task_id,
        "status": status,
        "run_id": run_id,
        "exit_code": exit_code,
        "last_error_code": last_error_code,
        "started_by": started_by,
        "notify_url": redact_notify_url(notify_url),
    }
    if description is not None:
        payload["description"] = description
    if suite is not None:
        payload["suite"] = suite
    if elapsed_sec is not None:
        payload["elapsed_sec"] = elapsed_sec
    if peak_ram_gib is not None:
        payload["peak_ram_gib"] = peak_ram_gib
    if progress_pct is not None:
        payload["progress_pct"] = progress_pct
    if block_reason is not None:
        payload["block_reason"] = block_reason
    if stop_reason is not None:
        payload["stop_reason"] = stop_reason
    if submitted_by:
        payload["submitted_by"] = submitted_by
    if submitted_via:
        payload["submitted_via"] = submitted_via
    if detail:
        payload["detail"] = detail
    return payload


_PROV_LOCAL = threading.local()


def _provenance_conn(path: Path):
    """One read-only connection per thread and DB path (was a full open_db per event)."""
    import sqlite3

    cache = getattr(_PROV_LOCAL, "conns", None)
    if cache is None:
        cache = _PROV_LOCAL.conns = {}
    key = str(path)
    conn = cache.get(key)
    if conn is None:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        cache[key] = conn
    return conn


def _lookup_provenance(home: Path, project: str, task_id: str) -> tuple[str | None, str | None]:
    """(submitted_by, submitted_via) for a task, or (None, None). Fail-open."""
    try:
        from taskqueue import config as _cfgmod

        path = _cfgmod.db_path(Path(home))
        if not path.is_file():
            return None, None
        try:
            row = _provenance_conn(path).execute(
                "SELECT submitted_by, submitted_via FROM tasks WHERE project = ? AND task_id = ?",
                (project, task_id),
            ).fetchone()
        except Exception:
            # Stale cached handle (DB replaced/moved): drop it; the next event reconnects.
            getattr(_PROV_LOCAL, "conns", {}).pop(str(path), None)
            raise
        if row is None:
            return None, None
        return row["submitted_by"] or None, row["submitted_via"] or None
    except Exception:
        return None, None


def event_matches_operator(ev: dict[str, Any], operator: str) -> bool:
    """An operator owns an event if they started the run or submitted the task."""
    return operator in (str(ev.get("started_by") or ""), str(ev.get("submitted_by") or ""))


def append_notify_event(home: Path, payload: dict[str, Any]) -> None:
    path = notify_log_path(home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, default=str) + "\n")
    except OSError as exc:
        log.warning("notify.jsonl append failed: %s", exc)


def _attempt_post(url: str, payload: dict[str, Any], *, allow_remote: bool = False) -> None:
    """One POST attempt; raises on any failure."""
    body = json.dumps(payload, default=str).encode("utf-8")
    req = Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    # The opener re-validates the host on every redirect *before* the next request is sent.
    opener = _make_no_redirect_opener(allow_remote=allow_remote)
    with opener.open(req, timeout=_POST_TIMEOUT_SEC) as resp:
        resp.read()


def record_dead_letter(home: Path, url: str, payload: dict[str, Any], error: str, attempts: int) -> None:
    """Persist a permanently failed webhook in ``notify_dead`` (best effort)."""
    try:
        from taskqueue import config as _cfgmod
        from taskqueue.db import open_write_conn

        path = _cfgmod.db_path(Path(home))
        if not path.is_file():
            return
        conn = open_write_conn(path, timeout=5.0)
        try:
            conn.execute(
                "INSERT INTO notify_dead (url, payload, error, attempts, failed_at) VALUES (?,?,?,?,?)",
                (url, json.dumps(payload, default=str), error[:500], attempts, _now_iso()),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        log.warning("notify_dead insert failed: %s", exc)


def list_dead_letters(conn, *, limit: int = 100) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, url, payload, error, attempts, failed_at FROM notify_dead ORDER BY id DESC LIMIT ?",
        (max(1, min(int(limit), 500)),),
    ).fetchall()
    out = []
    for r in rows:
        try:
            payload = json.loads(r["payload"])
        except ValueError:
            payload = {}
        if isinstance(payload, dict) and payload.get("notify_url"):
            payload = dict(payload)
            payload["notify_url"] = redact_notify_url(str(payload["notify_url"]))
        out.append({
            "id": r["id"],
            "url": redact_notify_url(r["url"] or ""),
            "error": r["error"],
            "attempts": r["attempts"],
            "failed_at": r["failed_at"],
            "payload": payload,
        })
    return out


def retry_dead_letter(conn, dead_id: int, *, allow_remote: bool = False) -> dict[str, Any]:
    """Re-POST one dead letter (single attempt, URL re-validated); delete it on success."""
    row = conn.execute("SELECT url, payload FROM notify_dead WHERE id = ?", (dead_id,)).fetchone()
    if row is None:
        return {"ok": False, "error": "not found", "status": 404}
    try:
        url = validate_notify_url(row["url"], allow_remote=allow_remote)
        _attempt_post(url, json.loads(row["payload"]), allow_remote=allow_remote)
    except Exception as exc:
        conn.execute("UPDATE notify_dead SET error = ?, attempts = attempts + 1 WHERE id = ?",
                     (str(exc)[:500], dead_id))
        conn.commit()
        return {"ok": False, "error": str(exc), "status": 502}
    conn.execute("DELETE FROM notify_dead WHERE id = ?", (dead_id,))
    conn.commit()
    return {"ok": True, "id": dead_id}


def _post_webhook(url: str, payload: dict[str, Any], home: Path | None = None, *, allow_remote: bool = False) -> None:
    """POST with retry. On permanent failure appends to notify_failed.jsonl."""
    last_exc: Exception | None = None
    for attempt in range(_POST_RETRIES):
        if attempt > 0:
            time.sleep(_POST_RETRY_DELAYS[attempt - 1])
        try:
            _attempt_post(url, payload, allow_remote=allow_remote)
            return  # success
        except Exception as exc:
            last_exc = exc
            log.warning("notify webhook POST attempt %d/%d failed url=%s: %s", attempt + 1, _POST_RETRIES, url, exc)
    # All retries exhausted — write to dead-letter file
    log.error("notify webhook permanently failed url=%s after %d attempts", url, _POST_RETRIES)
    if home is not None:
        record_dead_letter(home, url, payload, str(last_exc), _POST_RETRIES)
        dead = notify_failed_log_path(home)
        try:
            record = dict(payload, _failed_at=_now_iso(), _url=url, _error=str(last_exc))
            dead.parent.mkdir(parents=True, exist_ok=True)
            with dead.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, default=str) + "\n")
        except OSError as exc2:
            log.warning("notify_failed.jsonl append failed: %s", exc2)


def winotify_available() -> bool:
    """True if the optional winotify package can be imported."""
    try:
        import winotify  # noqa: F401
        return True
    except ImportError:
        return False


_LEVEL_RGB = {"red": (239, 68, 68), "amber": (245, 158, 11), "green": (34, 197, 94)}


def level_icon_path(level: str) -> str | None:
    """Absolute path to a 64×64 PNG with a centred circle (half the canvas) in the health colour.

    Written once to temp; Windows scales toast icons to a fixed box, so the transparent margin
    is what keeps the dot small.
    """
    import struct
    import tempfile
    import zlib

    rgb = _LEVEL_RGB.get(level)
    if rgb is None:
        return None
    path = Path(tempfile.gettempdir()) / "taskqueue_icons" / f"{level}_r15.png"
    if path.is_file():
        return str(path)
    size, r2 = 64, 15 * 15
    rows = b"".join(
        b"\x00" + b"".join(
            bytes((*rgb, 255)) if (x - 31.5) ** 2 + (y - 31.5) ** 2 <= r2 else b"\x00\x00\x00\x00"
            for x in range(size)
        )
        for y in range(size)
    )

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows, 9))
        + chunk(b"IEND", b"")
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(png)
    except OSError:
        return None
    return str(path)


def _send_toast(
    title: str, message: str, icon_path: str | None = None, *, level: str | None = None
) -> None:
    """Send a Windows toast notification. No-ops if winotify is absent or not on Windows.

    ``level`` (red / amber / green, see taskqueue.health) prefixes the title with the
    matching coloured emoji and uses a coloured-circle icon.
    """
    global _winotify_missing_logged, _toast_fail_logged
    if level:
        from taskqueue.health import LEVEL_ICONS

        title = f"{LEVEL_ICONS.get(level, '')} {title}".strip()
        icon_path = icon_path or level_icon_path(level)
    try:
        from winotify import Notification, audio  # type: ignore[import]

        n = Notification(
            app_id="taskqueue",
            title=title,
            msg=message,
            duration="short",
            icon=icon_path or "",
        )
        n.set_audio(audio.Default, loop=False)
        n.show()
    except ImportError:
        if not _winotify_missing_logged:
            _winotify_missing_logged = True
            log.warning(
                "winotify not installed; Windows toasts disabled — "
                "pip install winotify  (or: pip install 'taskqueue[toast]')"
            )
    except Exception as exc:
        if not _toast_fail_logged:
            _toast_fail_logged = True
            log.warning("Windows toast failed: %s", exc)


def emit_task_notify(
    home: Path,
    *,
    project: str,
    task_id: str,
    status: str,
    run_id: int | None,
    exit_code: int | None,
    last_error_code: str | None,
    notify_url: str | None,
    started_by: str | None,
    allow_remote: bool = False,
    post: bool = True,
    cfg: dict[str, Any] | None = None,
    # enriched fields
    description: str | None = None,
    suite: str | None = None,
    elapsed_sec: float | None = None,
    peak_ram_gib: float | None = None,
    progress_pct: float | None = None,
    block_reason: str | None = None,
    stop_reason: str | None = None,
) -> dict[str, Any] | None:
    """Append jsonl + fire-and-forget POST when notify_url is set and status is terminal.

    Returns the payload if emitted, else None. Never raises.

    notify_url resolution order:
      1. ``notify_url`` argument (per-task)
      2. ``cfg["notify_url"]`` (global config fallback)
    """
    _cfg = cfg or {}

    # Global notify_url fallback
    url = (notify_url or "").strip()
    if not url:
        url = str(_cfg.get("notify_url") or "").strip()

    toast_enabled = _cfg.get("toast_notifications", True)
    # Normalise "false"/"0" strings coming from config store
    if isinstance(toast_enabled, str):
        toast_enabled = toast_enabled.lower() not in ("false", "0", "no")

    if status not in TERMINAL_STATUSES:
        # Still send toast for terminal-like check (but skip webhook/jsonl)
        if toast_enabled and status == "started":
            _send_toast(f"tq: {task_id} started", project)
        return None

    # --- webhook + jsonl path ---
    validated_url: str | None = None
    if url:
        try:
            validated_url = validate_notify_url(url, allow_remote=allow_remote)
        except ValueError as exc:
            log.warning("skip notify for %s/%s: %s", project, task_id, exc)

    # notify_default (config, default on): append to notify.jsonl for every terminal task
    # even when no per-task or global notify_url is set; notify_url only gates the webhook.
    notify_default = _cfg.get("notify_default", True)  # terminal events always readable via drain/wait
    if isinstance(notify_default, str):
        notify_default = notify_default.lower() not in ("false", "0", "no", "")

    if validated_url or notify_default:
        _sub_by, _sub_via = _lookup_provenance(home, project, task_id)
        payload = build_notify_payload(
            project=project,
            task_id=task_id,
            status=status,
            run_id=run_id,
            exit_code=exit_code,
            last_error_code=last_error_code,
            started_by=started_by,
            notify_url=validated_url or "",
            description=description,
            suite=suite,
            elapsed_sec=elapsed_sec,
            peak_ram_gib=peak_ram_gib,
            progress_pct=progress_pct,
            block_reason=block_reason,
            stop_reason=stop_reason,
            submitted_by=_sub_by,
            submitted_via=_sub_via,
        )
        append_notify_event(home, payload)
        if validated_url and post:
            _ensure_webhook_worker()
            try:
                _WEBHOOK_QUEUE.put_nowait((validated_url, payload, home, allow_remote))
            except queue.Full:
                # F-145: durable dead-letter instead of silent drop.
                log.warning("notify queue full; dead-letter webhook for %s/%s", project, task_id)
                record_dead_letter(home, validated_url, payload, "webhook queue full", 0)
    else:
        payload = None

    # --- toast ---
    if toast_enabled:
        msg_parts = [project]
        if elapsed_sec is not None:
            msg_parts.append(f"elapsed={elapsed_sec:.1f}s")
        if last_error_code:
            msg_parts.append(f"error={last_error_code}")
        if exit_code is not None and exit_code != 0:
            msg_parts.append(f"exit={exit_code}")
        level = (
            "red" if status in ("failed", "unknown")
            else "green" if status == "completed"
            else "amber"
        )
        _send_toast(f"tq: {task_id} {status}", ", ".join(msg_parts), level=level)

    return payload


def notify_task_started(
    home: Path,
    *,
    project: str,
    task_id: str,
    run_id: int | None,
    started_by: str | None,
    notify_url: str | None,
    allow_remote: bool = False,
    post: bool = True,
    cfg: dict[str, Any] | None = None,
    description: str | None = None,
    suite: str | None = None,
) -> dict[str, Any] | None:
    """Fire a 'started' notification (jsonl + webhook + toast).

    Returns the payload if a webhook was emitted, else None. Never raises.
    """
    _cfg = cfg or {}
    url = (notify_url or "").strip()
    if not url:
        url = str(_cfg.get("notify_url") or "").strip()

    toast_enabled = _cfg.get("toast_notifications", True)
    if isinstance(toast_enabled, str):
        toast_enabled = toast_enabled.lower() not in ("false", "0", "no")

    payload: dict[str, Any] | None = None
    if url:
        try:
            validated_url = validate_notify_url(url, allow_remote=allow_remote)
        except ValueError as exc:
            log.warning("skip start-notify for %s/%s: %s", project, task_id, exc)
            validated_url = None

        if validated_url:
            payload = build_notify_payload(
                project=project,
                task_id=task_id,
                status="started",
                run_id=run_id,
                exit_code=None,
                last_error_code=None,
                started_by=started_by,
                notify_url=validated_url,
                description=description,
                suite=suite,
            )
            # F-052: do NOT append "started" to notify.jsonl — drain_notifications
            # is documented as terminal-only; non-terminal events pollute the MCP queue
            if post:
                _ensure_webhook_worker()
                try:
                    _WEBHOOK_QUEUE.put_nowait((validated_url, payload, home, allow_remote))
                except queue.Full:
                    log.warning(
                        "notify queue full; dead-letter start-webhook for %s/%s",
                        project, task_id,
                    )
                    record_dead_letter(home, validated_url, payload, "webhook queue full", 0)

    if toast_enabled:
        msg_parts = [project]
        if description:
            msg_parts.append(description)
        _send_toast(f"tq: {task_id} started", ", ".join(msg_parts))

    return payload


def _notify_cursor_path(home: Path, operator: str | None) -> Path:
    return Path(home) / "notify_cursor.json"


def _read_notify_cursor(home: Path, operator: str | None) -> int:
    try:
        data = json.loads(_notify_cursor_path(home, operator).read_text(encoding="utf-8"))
        return int(data.get(operator or "_all", 0))
    except Exception:
        return 0


_CURSOR_LOCK = threading.Lock()


def _write_notify_cursor(home: Path, operator: str | None, offset: int) -> None:
    p = _notify_cursor_path(home, operator)
    key = operator or "_all"
    with _CURSOR_LOCK:  # serialize the read-modify-write across handler/MCP threads
        try:
            try:
                existing = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                existing = {}
            existing[key] = offset
            tmp = p.with_name(f"{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            tmp.write_text(json.dumps(existing), encoding="utf-8")
            tmp.replace(p)
        except Exception:
            pass


def drain_notifications(
    home: Path,
    *,
    since: str | None = None,
    operator: str | None = None,
    project: str | None = None,
    limit: int | None = None,
    incremental: bool = False,
    wait_sec: float = 0.0,
) -> dict[str, Any]:
    """Read notify.jsonl events strictly after ``since`` (ISO ts string compare).

    ``wait_sec`` > 0 long-polls: if nothing matches yet, re-check every 0.5 s until an
    event arrives or the time is up (capped at 100 s; callers should run it off the read loop).

    When ``incremental=True``, reads only bytes since the last call via a
    per-operator byte cursor stored in ``home/notify_cursor.json``, reducing
    cost from O(2 MiB) to O(new bytes). Pass ``since=None`` for this to work
    correctly; passing an explicit ``since`` disables the cursor path.
    The MCP server passes ``incremental=True`` explicitly.
    """
    deadline = time.monotonic() + min(max(float(wait_sec or 0.0), 0.0), 100.0)
    while True:
        out = _drain_once(
            home, since=since, operator=operator, project=project, limit=limit, incremental=incremental
        )
        if out["count"] or time.monotonic() >= deadline:
            return out
        time.sleep(min(0.5, max(0.05, deadline - time.monotonic())))


def _drain_once(
    home: Path,
    *,
    since: str | None,
    operator: str | None,
    project: str | None,
    limit: int | None,
    incremental: bool,
) -> dict[str, Any]:
    path = notify_log_path(home)
    events: list[dict[str, Any]] = []
    if not path.is_file():
        return {"events": events, "items": events, "count": 0, "path": str(path)}
    try:
        if incremental and since is None:
            events = list(
                _iter_notify_events_incremental(
                    path,
                    home=home,
                    since=since,
                    operator=operator,
                    project=project,
                    limit=limit if limit is not None else 500,
                )
            )
        else:
            events = list(
                iter_notify_events(
                    path,
                    since=since,
                    operator=operator,
                    project=project,
                    limit=limit if limit is not None else 500,
                )
            )
    except OSError as exc:
        raise RuntimeError(f"cannot read notify log: {exc}") from exc
    return {"events": events, "items": events, "count": len(events), "path": str(path)}


# Max bytes to reverse-scan from EOF when draining notify.jsonl.
_NOTIFY_SCAN_BYTES = 2 * 1024 * 1024
_NOTIFY_DEFAULT_LIMIT = 500


def _parse_event_line(raw: bytes) -> dict[str, Any] | None:
    """One notify.jsonl line as a dict, or None for blank/corrupt/non-object lines."""
    line = raw.decode("utf-8", errors="replace").strip()
    if not line:
        return None
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        return None
    return ev if isinstance(ev, dict) else None


def _event_wanted(ev: dict[str, Any], since: str | None, operator: str | None, project: str | None) -> bool:
    """Shared filter for both readers. ``since`` is exclusive (``ts > since``) so MCP and web agree."""
    if since and str(ev.get("ts") or "") <= since:
        return False
    op = (operator or "").strip()
    if op and not event_matches_operator(ev, op):
        return False
    proj = (project or "").strip()
    return not proj or str(ev.get("project") or "") == proj


def iter_notify_events(
    path: Path,
    *,
    since: str | None = None,
    operator: str | None = None,
    project: str | None = None,
    limit: int = _NOTIFY_DEFAULT_LIMIT,
) -> list[dict[str, Any]]:
    """Return recent notify events matching filters (newest last).

    Reads at most ``_NOTIFY_SCAN_BYTES`` from the end of the file. Uses an
    exclusive ``since`` cursor (``ts > since``) so MCP and web agree.
    """
    limit = max(1, int(limit))
    try:
        size = path.stat().st_size
    except OSError:
        return []
    if size <= 0:
        return []
    start = max(0, size - _NOTIFY_SCAN_BYTES)
    with open(path, "rb") as fh:
        fh.seek(start)
        data = fh.read()
    if start > 0:
        nl = data.find(b"\n")
        if nl != -1:
            data = data[nl + 1 :]
    matched = [
        ev for ev in map(_parse_event_line, data.splitlines())
        if ev is not None and _event_wanted(ev, since, operator, project)
    ]
    if len(matched) > limit:
        matched = matched[-limit:]
    return matched


def _iter_notify_events_incremental(
    path: Path,
    *,
    home: Path,
    since: str | None = None,
    operator: str | None = None,
    project: str | None = None,
    limit: int = _NOTIFY_DEFAULT_LIMIT,
) -> list[dict[str, Any]]:
    """Read only new bytes since the last drain call (byte-cursor approach)."""
    limit = max(1, int(limit))
    try:
        size = path.stat().st_size
    except OSError:
        return []
    if size <= 0:
        _write_notify_cursor(home, operator, 0)
        return []

    last_offset = _read_notify_cursor(home, operator)
    if last_offset > size:
        last_offset = 0

    # Read from one byte early so we can tell whether the cursor sits on a line boundary
    # (it normally does); only then is the first line after it complete and kept.
    start = max(0, last_offset - 1)
    with open(path, "rb") as fh:
        fh.seek(start)
        raw = fh.read()
    base = start
    if last_offset > 0:
        if raw[:1] == b"\n":
            skip = 1
        else:  # cursor landed mid-line: drop the partial line
            nl = raw.find(b"\n")
            skip = nl + 1 if nl != -1 else len(raw)
        raw = raw[skip:]
        base += skip
    # Consume complete lines only; a half-written trailing line waits for the next call.
    last_nl = raw.rfind(b"\n")
    data = raw[: last_nl + 1] if last_nl != -1 else b""

    matched: list[dict[str, Any]] = []
    consumed = 0  # bytes of ``data`` covered by the events we return
    for bline in data.splitlines(keepends=True):
        if len(matched) >= limit:
            break  # leave the rest for the next drain: the cursor stops here
        consumed += len(bline)
        ev = _parse_event_line(bline)
        if ev is not None and _event_wanted(ev, since, operator, project):
            matched.append(ev)
    _write_notify_cursor(home, operator, base + consumed)
    return matched
