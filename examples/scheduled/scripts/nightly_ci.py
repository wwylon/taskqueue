"""Scheduled nightly smoke — uses tq_client progress."""
from __future__ import annotations

import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT / "clients" / "python"))

from tq_client import progress  # noqa: E402


def main() -> int:
    for i in range(0, 3):
        progress(100.0 * (i + 1) / 3, f"nightly {i + 1}/3")
        time.sleep(0.05)
    progress(100.0, "nightly done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
