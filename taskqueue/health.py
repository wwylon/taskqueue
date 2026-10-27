"""One red / amber / green verdict for the whole queue (web header, ``tq health``, status lines).

* **red**   — a task failed (or ended ``unknown``) after the last acknowledge, or the daemon is down.
* **amber** — acknowledged failures still present, a cap-``interrupted`` task, or a frozen run.
* **green** — nothing needs a human.

Acknowledging stores a timestamp in ``{TASKQUEUE_HOME}/health_ack.json``; failures that ended at
or before it drop from red to amber until they are retried, disabled or removed.
Disabled tasks (``enabled = 0``) are shelved on purpose and never count.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from taskqueue.db import blocked_projects

ACK_FILE = "health_ack.json"
FAILED_STATUSES = ("failed", "unknown")
_SAMPLE = 5

# Emoji carry the colour where ANSI cannot (browser tab titles, notifications).
LEVEL_ICONS = {"red": "\U0001F534", "amber": "\U0001F7E0", "green": "\U0001F7E2"}  # 🔴 🟠 🟢
PART_ICONS = {
    "daemon_down": "\U0001F480",      # 💀
    "new_failed": "❌",           # ❌
    "failed": "⚠️",         # ⚠️
    "frozen": "\U0001F9CA",           # 🧊
    "interrupted": "⏸️",    # ⏸️
    "running": "▶️",        # ▶️
    "queued": "⏳",               # ⏳
}
_PLAIN_LEVEL = {"red": "[!!]", "amber": "[! ]", "green": "[ok]"}
_ANSI = {"red": "\033[31m", "amber": "\033[33m", "green": "\033[32m"}
_RESET = "\033[0m"


def read_ack(home: Path) -> str | None:
    try:
        data = json.loads((home / ACK_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    ack = data.get("ack_at") if isinstance(data, dict) else None
    return ack if isinstance(ack, str) else None


def write_ack(home: Path, operator: str | None = None, *, now: datetime | None = None) -> str:
    ack_at = (now or datetime.now(UTC)).isoformat()
    tmp = home / (ACK_FILE + ".tmp")
    tmp.write_text(json.dumps({"ack_at": ack_at, "by": operator}), encoding="utf-8")
    tmp.replace(home / ACK_FILE)
    return ack_at


def compute_health(
    conn: sqlite3.Connection,
    *,
    ack_at: str | None = None,
    daemon_up: bool | None = None,
) -> dict[str, Any]:
    """Health verdict from the tasks table. ``daemon_up=None`` means "not checked".

    Frozen or paused projects are left out entirely: their tasks are parked on purpose.
    """
    rows = conn.execute(
        # A run's finish time, not tasks.updated_at: sync and block notes touch updated_at
        # (which re-raised acknowledged failures that have no run row).
        "SELECT t.project, t.task_id, t.status, t.last_error_code, "
        "COALESCE(r.finished_at, t.finished_at, t.updated_at) AS ended_at "
        "FROM tasks t LEFT JOIN runs r ON r.run_id = t.last_run_id "
        "WHERE t.enabled = 1 AND t.status IN ('failed', 'unknown', 'interrupted', 'queued', 'running') "
        "ORDER BY ended_at DESC"
    ).fetchall()
    blocked = set(blocked_projects(conn))
    failed: list[str] = []
    new_failed: list[str] = []
    interrupted: list[str] = []
    frozen: list[str] = []
    running = queued = 0
    for r in rows:
        if r["project"] in blocked:
            continue
        key = f"{r['project']}/{r['task_id']}"
        status = r["status"]
        if status in FAILED_STATUSES:
            failed.append(key)
            if ack_at is None or (r["ended_at"] or "") > ack_at:
                new_failed.append(key)
        elif status == "interrupted":
            interrupted.append(key)
        elif status == "running":
            running += 1
            if r["last_error_code"] == "tq.run.frozen":
                frozen.append(key)
        elif status == "queued":
            queued += 1

    level = (
        "red" if daemon_up is False or new_failed
        else "amber" if failed or interrupted or frozen
        else "green"
    )
    acked = len(failed) - len(new_failed)
    parts = [
        {"kind": kind, "count": n, "text": text}
        for kind, n, text in (
            ("daemon_down", int(daemon_up is False), "daemon down"),
            ("new_failed", len(new_failed), f"{len(new_failed)} new failed"),
            ("failed", acked, f"{acked} failed (acknowledged)"),
            ("frozen", len(frozen), f"{len(frozen)} frozen"),
            ("interrupted", len(interrupted), f"{len(interrupted)} interrupted"),
        )
        if n
    ]
    return {
        "level": level,
        "summary": ", ".join(p["text"] for p in parts) or "ok",
        "parts": parts,
        "counts": {
            "new_failed": len(new_failed),
            "failed": len(failed),
            "interrupted": len(interrupted),
            "frozen": len(frozen),
            "running": running,
            "queued": queued,
        },
        "new_failed": new_failed[:_SAMPLE],
        "failed": failed[:_SAMPLE],
        "interrupted": interrupted[:_SAMPLE],
        "frozen": frozen[:_SAMPLE],
        "ack_at": ack_at,
        "daemon_up": daemon_up,
        "icons": {"level": LEVEL_ICONS, "parts": PART_ICONS},
    }


def format_line(health: dict[str, Any], *, icons: bool = True, color: bool = False) -> str:
    """One status-line string.

    ``icons``: ``🔴 tq ❌2 ⚠️10 ⏸️2 · ▶️3 ⏳4`` (the icon says what each count is).
    Plain: ``[!!] tq 2 new failed, 10 failed (acknowledged) | 3 running | 4 queued``;
    ``color`` adds ANSI to the plain level marker (emoji are already coloured).
    """
    c = health["counts"]
    level = health["level"]
    live = [("running", c["running"]), ("queued", c["queued"])]
    if icons:
        problems = " ".join(
            PART_ICONS[p["kind"]] + ("" if p["kind"] == "daemon_down" else str(p["count"]))
            for p in health["parts"]
        )
        activity = " ".join(f"{PART_ICONS[k]}{n}" for k, n in live if n)
        sep = "·" if problems and activity else ""
        return " ".join(x for x in (LEVEL_ICONS[level] + " tq", problems, sep, activity) if x)
    marker = _PLAIN_LEVEL[level]
    if color:
        marker = f"{_ANSI[level]}{marker}{_RESET}"
    bits = [health["summary"]] if health["parts"] else []
    bits += [f"{n} {k}" for k, n in live if n or k == "running"]
    return f"{marker} tq " + " | ".join(bits)  # ASCII: plain mode is for consoles without UTF-8
