"""Progress / freeze helpers using process active CPU time (not wall idle alone)."""

from __future__ import annotations

from typing import Any

import psutil


def process_active_cpu_sec(pid: int | None) -> float | None:
    """Sum user+system CPU seconds for pid and children. None if process gone."""
    if not pid:
        return None
    try:
        parent = psutil.Process(pid)
        total = 0.0
        for p in [parent, *parent.children(recursive=True)]:
            try:
                t = p.cpu_times()
                total += float(t.user) + float(t.system)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return total
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


def is_output_frozen(idle_stdout_sec: float, threshold_sec: float) -> bool:
    return idle_stdout_sec > threshold_sec


def is_active_frozen(active_idle_sec: float, threshold_sec: float) -> bool:
    """No meaningful CPU burn for longer than threshold."""
    return active_idle_sec > threshold_sec


def is_frozen(
    idle_stdout_sec: float,
    active_idle_sec: float,
    threshold_sec: float,
) -> bool:
    """Frozen when both stdout and active CPU have been idle past threshold."""
    return is_output_frozen(idle_stdout_sec, threshold_sec) and is_active_frozen(
        active_idle_sec, threshold_sec
    )


def needs_frozen_attention(idle_sec: float, threshold_sec: float) -> bool:
    """At 2× frozen_threshold, surface attention (do not auto-kill)."""
    return idle_sec >= 2.0 * threshold_sec


def progress_is_suspect(
    prev_pct: float | None,
    cur_pct: float | None,
    prev_active: float | None,
    cur_active: float | None,
    *,
    min_pct_delta: float = 5.0,
    max_active_delta: float = 0.05,
    gpu_requested: bool = False,
    wsl_dispatched: bool = False,
) -> bool:
    """True when % advances without corresponding process CPU time.

    Host CPU-time is not a valid "is this task doing real work" signal for a
    GPU-bound task: it can spend most of its time blocked on
    cudaStreamSynchronize/DMA with near-zero host CPU while genuinely
    progressing. A task that declares gpu_vram_gib in its resource request
    skips this CPU-based heuristic entirely and relies on `is_frozen`
    (no stdout AND no CPU) instead, which doesn't share this false-positive
    mode since a truly stalled task eventually stops producing output too.

    WSL-dispatched tasks (command ``wsl``) are also skipped: psutil on Windows
    cannot enumerate CPU time for processes inside WSL, so host active_delta
    stays near zero while the guest is doing real work (F-244).
    """
    if gpu_requested or wsl_dispatched:
        return False
    if prev_pct is None or cur_pct is None:
        return False
    if prev_active is None or cur_active is None:
        return False
    pct_delta = float(cur_pct) - float(prev_pct)
    if pct_delta < min_pct_delta:
        return False
    active_delta = float(cur_active) - float(prev_active)
    return active_delta <= max_active_delta


def sanity_snapshot(
    *,
    idle_stdout_sec: float,
    active_idle_sec: float,
    threshold_sec: float,
    prev_pct: float | None,
    cur_pct: float | None,
    prev_active: float | None,
    cur_active: float | None,
    gpu_requested: bool = False,
    wsl_dispatched: bool = False,
) -> dict[str, Any]:
    frozen = is_frozen(idle_stdout_sec, active_idle_sec, threshold_sec)
    # Attention uses the stricter of stdout/active idle once already frozen.
    idle_for_attn = max(idle_stdout_sec, active_idle_sec) if frozen else 0.0
    return {
        "frozen": frozen,
        "frozen_attention": frozen and needs_frozen_attention(idle_for_attn, threshold_sec),
        "progress_suspect": progress_is_suspect(
            prev_pct,
            cur_pct,
            prev_active,
            cur_active,
            gpu_requested=gpu_requested,
            wsl_dispatched=wsl_dispatched,
        ),
        "idle_stdout_sec": idle_stdout_sec,
        "active_idle_sec": active_idle_sec,
    }
