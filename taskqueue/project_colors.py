"""Per-project UI colors: each new project gets the random candidate farthest from colors in use.

Colors are stored as config keys ``project_color_<name>`` ("#rrggbb") so they survive restarts and
stay stable when projects come and go. Distance is CIE76 ΔE in CIELAB.
"""
from __future__ import annotations

import colorsys
import hashlib
import math
import random
import sqlite3

PREFIX = "project_color_"
N_CANDIDATES = 200
# Saturation / lightness band that stays readable on both dark and light themes.
_S_RANGE = (0.55, 0.85)
_L_RANGE = (0.48, 0.62)


def _hex(r: float, g: float, b: float) -> str:
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


def _rgb(hexcol: str) -> tuple[float, float, float]:
    h = hexcol.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore[return-value]


def _lab(hexcol: str) -> tuple[float, float, float]:
    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (lin(c) for c in _rgb(hexcol))
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116

    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def delta_e(a: str, b: str) -> float:
    return math.dist(_lab(a), _lab(b))


def pick_color(used: list[str], seed: str, n: int = N_CANDIDATES) -> str:
    """Best of ``n`` random candidates: the one with the largest min-ΔE to ``used`` colors."""
    rng = random.Random(int(hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12], 16))
    best, best_d = "", -1.0
    for _ in range(max(1, n)):
        h = rng.random()
        s = rng.uniform(*_S_RANGE)
        light = rng.uniform(*_L_RANGE)
        col = _hex(*colorsys.hls_to_rgb(h, light, s))
        d = min((delta_e(col, u) for u in used), default=100.0)
        if d > best_d:
            best, best_d = col, d
    return best


def assign_project_colors(existing: dict[str, str], projects: list[str]) -> dict[str, str]:
    """Return ``existing`` plus a color for every project in ``projects`` that lacks one."""
    out = dict(existing)
    for p in sorted(projects):
        if p not in out:
            out[p] = pick_color(list(out.values()), p)
    return out


def hash_color(name: str) -> str:
    """Deterministic fallback for projects not yet assigned (used by the read path)."""
    h = int(hashlib.sha1(name.encode("utf-8")).hexdigest()[:8], 16) / 0xFFFFFFFF
    return _hex(*colorsys.hls_to_rgb(h, 0.55, 0.7))


def load_project_colors(cfg: dict) -> dict[str, str]:
    return {k[len(PREFIX):]: v for k, v in cfg.items() if k.startswith(PREFIX) and isinstance(v, str)}


def ensure_project_colors(conn: sqlite3.Connection, projects: list[str]) -> dict[str, str]:
    """Persist colors for projects without one; returns the full map."""
    from taskqueue.db import get_config, set_config

    cur = load_project_colors(get_config(conn))
    new = assign_project_colors(cur, [p for p in projects if p])
    for p, c in new.items():
        if p not in cur:
            set_config(conn, PREFIX + p, c)
    return new
