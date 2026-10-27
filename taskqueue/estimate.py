"""Resource estimation for admission control (scheduler_resources.md D1).

The scheduler must decide how much RAM/VRAM/CPU a task will *actually* use
before it starts. Declared YAML values are optional and frequently absent, so
an estimate is built from, in order of preference:

  1. declared    — explicit `ram_gib` / `gpu_vram_gib` / `cpu_cores` in the YAML
  2. history     — p95 of observed peaks over this task's recent runs
  3. sibling     — same for tasks sharing a `yaml_source` (matrix: expansions,
                   which produce N same-shaped tasks each with no own history)
  4. group       — same for tasks sharing a `group:`
  5. default     — a non-zero floor so an undeclared task is never free

The estimate is `max(declared, predicted, floor)` — we take the most
pessimistic signal available rather than trusting any single one.
"""

from __future__ import annotations

import math
import sqlite3
from typing import Any
from taskqueue.rows import row_get

# How many recent runs feed a prediction, unless config overrides.
DEFAULT_PREDICT_RUNS = 5

# Estimate provenance, ordered weakest to strongest confidence.
SOURCE_DEFAULT = "default"
SOURCE_GROUP = "group"
SOURCE_SIBLING = "sibling"
SOURCE_HISTORY = "history"
SOURCE_DECLARED = "declared"


def _p95(values: list[float]) -> float:
    """Conservative p95. For small samples this degenerates to max, which is
    the behaviour we want — with 5 points there is no meaningful tail."""
    vals = sorted(v for v in values if v is not None and v > 0)
    if not vals:
        return 0.0
    idx = math.ceil(0.95 * (len(vals) - 1))
    return float(vals[min(idx, len(vals) - 1)])


def _peaks_for(
    conn: sqlite3.Connection, where: str, params: tuple, limit: int
) -> tuple[list[float], list[float]]:
    """Return (ram_peaks, vram_peaks) for runs matching *where*."""
    rows = conn.execute(
        "SELECT r.peak_ram_gib, r.peak_vram_gib "
        "FROM runs r JOIN tasks t ON t.project = r.project AND t.task_id = r.task_id "
        f"WHERE {where} "
        "  AND (r.peak_ram_gib IS NOT NULL OR r.peak_vram_gib IS NOT NULL) "
        # A running run's peaks are partial; only finished runs predict the next one.
        "  AND r.status != 'running' "
        "ORDER BY r.run_id DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    ram = [float(r["peak_ram_gib"]) for r in rows if r["peak_ram_gib"] is not None]
    vram = [float(r["peak_vram_gib"]) for r in rows if r["peak_vram_gib"] is not None]
    return ram, vram


def prefetch_exact_peaks(
    conn: sqlite3.Connection,
    keys: list[tuple[str, str]] | set[tuple[str, str]],
    *,
    predict_runs: int = DEFAULT_PREDICT_RUNS,
) -> tuple[dict[tuple[str, str], dict[str, Any]], set[tuple[str, str]]]:
    """Bulk exact-task peak lookup for many (project, task_id) keys.

    Returns ``(peaks, checked)`` where ``peaks`` maps keys that have finished-run
    history to a predict dict, and ``checked`` is every requested key (so a miss
    in ``peaks`` means "no history", not "not prefetched"). Used by Timeline.
    """
    uniq = [(p, t) for p, t in dict.fromkeys(keys)]  # stable unique
    checked = set(uniq)
    if not uniq:
        return {}, checked
    limit = max(1, int(predict_runs))
    out: dict[tuple[str, str], dict[str, Any]] = {}
    chunk = 400
    for i in range(0, len(uniq), chunk):
        part = uniq[i : i + chunk]
        placeholders = ",".join("(?)" for _ in part)
        tokens = [f"{p}/{t}" for p, t in part]
        rows = conn.execute(
            "SELECT project, task_id, peak_ram_gib, peak_vram_gib FROM ("
            " SELECT r.project, r.task_id, r.peak_ram_gib, r.peak_vram_gib,"
            "  ROW_NUMBER() OVER (PARTITION BY r.project, r.task_id ORDER BY r.run_id DESC) AS rn"
            " FROM runs r"
            " WHERE r.status != 'running'"
            "  AND (r.peak_ram_gib IS NOT NULL OR r.peak_vram_gib IS NOT NULL)"
            f"  AND (r.project || '/' || r.task_id) IN ({placeholders})"
            f") WHERE rn <= {limit}",
            tokens,
        ).fetchall()
        buckets: dict[tuple[str, str], tuple[list[float], list[float]]] = {}
        for r in rows:
            key = (r["project"] or "", r["task_id"] or "")
            ram_l, vram_l = buckets.setdefault(key, ([], []))
            if r["peak_ram_gib"] is not None:
                ram_l.append(float(r["peak_ram_gib"]))
            if r["peak_vram_gib"] is not None:
                vram_l.append(float(r["peak_vram_gib"]))
        for key, (ram_l, vram_l) in buckets.items():
            if ram_l or vram_l:
                out[key] = {
                    "ram_gib": _p95(ram_l),
                    "vram_gib": _p95(vram_l),
                    "source": SOURCE_HISTORY,
                }
    return out, checked


def predict_task_resources(
    conn: sqlite3.Connection,
    project: str,
    task_id: str,
    *,
    predict_runs: int = DEFAULT_PREDICT_RUNS,
    exact_peaks: dict[tuple[str, str], dict[str, Any]] | None = None,
    exact_checked: set[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Predict peak RAM/VRAM from run history, widening the lookup as needed.

    Returns {"ram_gib", "vram_gib", "source"}. Zeros with source=default when
    nothing is known. Optional ``exact_peaks`` / ``exact_checked`` skip the
    per-task history query when Timeline already bulk-loaded those peaks.
    """
    limit = max(1, int(predict_runs))
    key = (project, task_id)

    # 1. This exact task.
    if exact_checked is not None and key in exact_checked:
        hit = (exact_peaks or {}).get(key)
        if hit:
            return hit
        # Prefetch covered this key and found no peaks — fall through to sibling/group.
    else:
        ram, vram = _peaks_for(
            conn, "r.project = ? AND r.task_id = ?", (project, task_id), limit
        )
        if ram or vram:
            return {"ram_gib": _p95(ram), "vram_gib": _p95(vram), "source": SOURCE_HISTORY}

    # 2. Siblings — tasks generated from the same YAML file. A `matrix:` block
    #    expands one file into N tasks (db.parse_task_yaml_many), so siblings
    #    are the same workload with different parameters: the best possible
    #    proxy when this particular task has never run.
    row = conn.execute(
        "SELECT yaml_source, grp FROM tasks WHERE project = ? AND task_id = ?",
        (project, task_id),
    ).fetchone()
    if row is not None:
        yaml_source = row["yaml_source"]
        if yaml_source:
            ram, vram = _peaks_for(
                conn,
                "t.yaml_source = ? AND NOT (r.project = ? AND r.task_id = ?)",
                (yaml_source, project, task_id),
                limit,
            )
            if ram or vram:
                return {
                    "ram_gib": _p95(ram),
                    "vram_gib": _p95(vram),
                    "source": SOURCE_SIBLING,
                }

        # 3. Same group — a weaker signal than siblings but better than nothing.
        grp = row_get(row, "grp")
        if grp:
            ram, vram = _peaks_for(
                conn,
                "t.project = ? AND t.grp = ? AND NOT (r.project = ? AND r.task_id = ?)",
                (project, grp, project, task_id),
                limit,
            )
            if ram or vram:
                return {
                    "ram_gib": _p95(ram),
                    "vram_gib": _p95(vram),
                    "source": SOURCE_GROUP,
                }

    return {"ram_gib": 0.0, "vram_gib": 0.0, "source": SOURCE_DEFAULT}


ESTIMATE_CACHE_MAX = 5000


def estimate_task_resources(
    conn: sqlite3.Connection,
    project: str,
    task_id: str,
    declared: dict[str, Any] | None = None,
    *,
    default_ram_gib: float = 1.0,
    predict_runs: int = DEFAULT_PREDICT_RUNS,
    cache: dict | None = None,
    exact_peaks: dict[tuple[str, str], dict[str, Any]] | None = None,
    exact_checked: set[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Best estimate of what this task will consume.

    `declared` is the task's parsed `resources` dict. Pass a dict as *cache* to
    memoise within one scheduler tick — this runs once per queued candidate.
    """
    _d = declared or {}
    # Declared resources are part of the key: editing a YAML must not serve a stale estimate.
    key = (
        project, task_id, float(default_ram_gib), int(predict_runs),
        float(_d.get("ram_gib") or 0.0), float(_d.get("gpu_vram_gib") or 0.0),
        float(_d.get("cpu_cores") or 1.0),
    )
    if cache is not None and key in cache:
        return cache[key]

    d = declared or {}
    dec_ram = float(d.get("ram_gib") or 0.0)
    dec_vram = float(d.get("gpu_vram_gib") or 0.0)
    dec_cpu = float(d.get("cpu_cores") or 1.0)

    pred = predict_task_resources(
        conn, project, task_id, predict_runs=predict_runs,
        exact_peaks=exact_peaks, exact_checked=exact_checked,
    )

    ram = max(dec_ram, pred["ram_gib"], float(default_ram_gib))
    vram = max(dec_vram, pred["vram_gib"])

    # Provenance reflects which signal actually set the value, so block_reason
    # and the admission log can explain the decision.
    if dec_ram >= ram and dec_ram > 0:
        source = SOURCE_DECLARED
    elif pred["ram_gib"] >= ram and pred["ram_gib"] > 0:
        source = pred["source"]
    else:
        source = SOURCE_DEFAULT

    result = {
        "ram_gib": round(ram, 3),
        "vram_gib": round(vram, 3),
        "cpu_cores": dec_cpu,
        "source": source,
        # True only for a blind guess: no RAM/VRAM declaration and no
        # history/sibling/group signal. Do not key off `source` — the default
        # floor can win the numeric estimate even when the task declared a
        # lower ram_gib or has sub-floor history, and those must not consume
        # the max_concurrent_unknown slot.
        "unknown": dec_ram <= 0 and dec_vram <= 0 and pred["source"] == SOURCE_DEFAULT,
    }
    if cache is not None:
        if len(cache) >= ESTIMATE_CACHE_MAX:
            cache.clear()  # bounded: a long-lived cross-tick cache must not grow forever
        cache[key] = result
    return result
