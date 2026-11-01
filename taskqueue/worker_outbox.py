"""Worker pending-finalize outbox helpers (F-208 extract from worker.py)."""

from __future__ import annotations

import json
import time
from pathlib import Path

PENDING_FINALIZE_DIR = "pending_finalize"


def pending_finalize_dir(home: Path) -> Path:
    return Path(home) / PENDING_FINALIZE_DIR


def write_pending_finalize(home: Path, payload: dict) -> Path:
    """Persist finalize payload before the HTTP attempt."""
    d = pending_finalize_dir(home)
    d.mkdir(parents=True, exist_ok=True)
    dest = d / f"{payload['run_id']}.json"
    tmp = dest.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(dest)
    return dest


def pending_finalize_is_ack(resp: dict | None) -> bool:
    """True if the master accepted finalize or the attempt is gone (do not retry)."""
    if not resp:
        return False
    if resp.get("ok"):
        return True
    err = str(resp.get("error") or "").lower()
    return any(
        s in err
        for s in (
            "already finalized",
            "not running",
            "run not owned",
            "finalize_token mismatch",
        )
    )


def pending_finalize_is_permanent_reject(resp: dict | None) -> bool:
    """4xx-style rejects that will never succeed on retry."""
    if not resp or resp.get("ok"):
        return False
    err = str(resp.get("error") or "").lower()
    return "invalid finalize status" in err or "unknown field" in err


def quarantine_pending_finalize(home: Path, path: Path, reason: str) -> Path:
    """Move a permanently-rejected outbox file aside so claims can resume."""
    qdir = pending_finalize_dir(home) / "quarantine"
    qdir.mkdir(parents=True, exist_ok=True)
    dest = qdir / path.name
    try:
        meta = {"reason": reason, "quarantined_at": time.time()}
        (qdir / f"{path.stem}.reason.json").write_text(json.dumps(meta), encoding="utf-8")
        path.replace(dest)
    except OSError:
        dest = path
    return dest
