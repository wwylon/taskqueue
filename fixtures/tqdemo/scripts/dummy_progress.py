"""Happy-path progress via tq_client."""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT / "clients" / "python"))

from tq_client import progress  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--steps", type=int, default=5)
    p.add_argument("--sleep", type=float, default=0.2)
    args = p.parse_args()
    for i in range(args.steps + 1):
        pct = 100.0 * i / args.steps
        progress(pct, f"dummy-progress step {i}/{args.steps}")
        if i < args.steps:
            time.sleep(args.sleep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
