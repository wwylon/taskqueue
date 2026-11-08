"""Idle under cooperative hold; progress continues after until expires."""
from __future__ import annotations

import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT / "clients" / "python"))

from tq_client import maybe_suspend, progress  # noqa: E402


def main() -> int:
    for i in range(0, 101, 10):
        maybe_suspend()  # honors hold (idles) and suspend (exit 75)
        progress(float(i), f"dummy-hold step {i}")
        time.sleep(0.25)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
