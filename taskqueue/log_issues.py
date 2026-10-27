"""Classify task log lines and keep warn/error/other counts in metrics_json."""

from __future__ import annotations

import json
import re
from typing import Any, Iterable

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# Log-level tokens, not English "error"/"errors" (``0 errors`` must not count).
_ERR_RE = re.compile(r"\b(?:ERROR|FATAL|CRITICAL|REJECT)\b")
_ERR_TITLE_RE = re.compile(r"\b(?:Error|Fatal|Critical|Reject):")
_WARN_RE = re.compile(r"\bWARN(?:ING)?\b", re.IGNORECASE)
_OTHER_RE = re.compile(r"LOW SCORE", re.IGNORECASE)

LogKind = str  # "errors" | "warnings" | "other"


def strip_ansi(line: str) -> str:
    return _ANSI_RE.sub("", line or "")


def classify_log_line(line: str) -> LogKind | None:
    """Return a count bucket, or None if the line is not an issue.

    First match wins. Error tokens are case-sensitive so prose like
    ``an error occurred`` / ``0 errors`` is ignored; ``WARN`` is not.
    """
    text = strip_ansi(line).strip()
    if not text:
        return None
    if _ERR_RE.search(text) or _ERR_TITLE_RE.search(text):
        return "errors"
    if _WARN_RE.search(text):
        return "warnings"
    if _OTHER_RE.search(text):
        return "other"
    return None


def count_log_issues(lines: Iterable[str]) -> dict[str, int]:
    """Return per-bucket increments for *lines* (zeros when nothing matched)."""
    inc = empty_log_counts()
    for line in lines:
        kind = classify_log_line(line)
        if kind:
            inc[kind] += 1
    return inc


def empty_log_counts() -> dict[str, int]:
    return {"warnings": 0, "errors": 0, "other": 0}


def coerce_log_counts(raw: Any) -> dict[str, int]:
    out = empty_log_counts()
    if not isinstance(raw, dict):
        return out
    for key in out:
        try:
            out[key] = max(0, int(raw.get(key) or 0))
        except (TypeError, ValueError):
            out[key] = 0
    return out


def nonzero_log(raw: Any) -> dict[str, int] | None:
    counts = coerce_log_counts(raw)
    if sum(counts.values()) <= 0:
        return None
    return counts


def log_from_metrics_json(raw: Any) -> dict[str, int]:
    payload = _as_metrics_dict(raw)
    return coerce_log_counts(payload.get("log"))


def apply_log_lines_to_metrics(metrics: dict[str, Any] | None, lines: Iterable[str]) -> dict[str, Any]:
    """Increment ``metrics['log']`` for each matching line. Reserved key ``log``."""
    payload = dict(metrics or {})
    log = coerce_log_counts(payload.get("log"))
    inc = count_log_issues(lines)
    for key, n in inc.items():
        log[key] += n
    payload["log"] = log
    return payload


def apply_log_lines_to_metrics_json(raw: Any, lines: Iterable[str]) -> str:
    payload = apply_log_lines_to_metrics(_as_metrics_dict(raw), lines)
    return json.dumps(payload, ensure_ascii=False)


def apply_worker_chunk(raw: Any, lines: list[str], prev_pct: float | None, now: float) -> str | None:
    """Master-side metrics update for a remote run's log chunk: log counts, TQ_METRIC values and
    their series, and progress timing (same reserved keys as local runs). None when nothing changed."""
    import re

    from taskqueue.runner import _parse_pct

    metric_re = re.compile(r"^TQ_METRIC\s+(\{.+\})\s*$")
    payload = _as_metrics_dict(raw)
    changed = False
    inc = count_log_issues(lines)
    if sum(inc.values()):
        payload = apply_log_lines_to_metrics(payload, lines)
        changed = True
    series = payload.get("_series") if isinstance(payload.get("_series"), dict) else {}
    timing = payload.get("_progress") if isinstance(payload.get("_progress"), dict) else None
    pct_prev = prev_pct
    for line in lines:
        text = line.rstrip()
        m = metric_re.match(text)
        if m:
            try:
                vals = json.loads(m.group(1))
            except ValueError:
                vals = None
            if isinstance(vals, dict):
                for k in ("log", "_series", "_progress"):
                    vals.pop(k, None)
                payload.update(vals)
                record_metric_samples(series, vals, now)
                changed = True
            continue
        pct = _parse_pct(text)
        if pct is not None and pct != pct_prev:
            timing = note_progress(timing, pct_prev, pct, now)
            pct_prev = pct
            changed = True
    if series:
        payload["_series"] = series
    if timing:
        payload["_progress"] = timing
    return json.dumps(payload, ensure_ascii=False) if changed else None


SERIES_MAX_KEYS = 8
SERIES_MAX_POINTS = 60


def record_metric_samples(series: dict[str, list], metrics: dict[str, Any], ts: float) -> None:
    """Append numeric TQ_METRIC values as ``[ts, value]`` points (last 60 per key, at most 8 keys)."""
    for key, val in metrics.items():
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            continue
        if key not in series and len(series) >= SERIES_MAX_KEYS:
            continue
        pts = series.setdefault(str(key), [])
        pts.append([round(ts, 1), float(val)])
        if len(pts) > SERIES_MAX_POINTS:
            del pts[: len(pts) - SERIES_MAX_POINTS]


def note_progress(timing: dict | None, prev_pct: float | None, pct: float, now: float) -> dict:
    """Progress timing for stalled-vs-slow: when % last moved, typical interval and recent rate (EMA).

    ``{"at": epoch, "interval": sec, "rate": pct_per_sec}``; interval/rate need two changes.
    """
    t = dict(timing or {})
    last = t.get("at")
    if last is not None and prev_pct is not None and now > last:
        dt = now - float(last)
        rate = (float(pct) - float(prev_pct)) / dt
        a = 0.3
        t["interval"] = dt if t.get("interval") is None else a * dt + (1 - a) * float(t["interval"])
        if rate > 0:
            t["rate"] = rate if t.get("rate") is None else a * rate + (1 - a) * float(t["rate"])
    t["at"] = round(now, 1)
    return t


def progress_health(timing: dict | None, pct: float | None, now: float, *, k: float = 3.0, floor_sec: float = 120.0) -> dict:
    """``{age_sec, stalled, eta_sec}`` from :func:`note_progress` data. ``stalled`` when the last % change is
    older than ``k`` x the usual interval (and at least ``floor_sec``); ETA uses the recent rate."""
    if not timing or timing.get("at") is None:
        return {}
    age = max(0.0, now - float(timing["at"]))
    out: dict[str, Any] = {"age_sec": round(age)}
    interval = timing.get("interval")
    if interval:
        out["stalled"] = age > max(k * float(interval), floor_sec)
    rate = timing.get("rate")
    if rate and pct is not None and 0 <= float(pct) < 100:
        out["eta_sec"] = round((100.0 - float(pct)) / float(rate))
    return out


def merge_metrics_payload(task_metrics: dict[str, Any] | None, log_counts: Any, series: dict | None = None,
                          progress: dict | None = None) -> dict[str, Any]:
    """TQ_METRIC fields plus reserved ``log``, ``_series`` and ``_progress``; a task cannot overwrite them."""
    payload = dict(task_metrics or {})
    payload.pop("log", None)
    payload.pop("_series", None)
    payload.pop("_progress", None)
    if progress:
        payload["_progress"] = dict(progress)
    nz = nonzero_log(log_counts)
    if nz:
        payload["log"] = nz
    if series:
        payload["_series"] = {k: list(v) for k, v in series.items()}
    return payload


def format_log_counts(raw: Any) -> str | None:
    """Human line ``warn=12 err=3 other=5``, or None when all zero."""
    nz = nonzero_log(raw)
    if not nz:
        return None
    return f"warn={nz['warnings']} err={nz['errors']} other={nz['other']}"


def _as_metrics_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}
