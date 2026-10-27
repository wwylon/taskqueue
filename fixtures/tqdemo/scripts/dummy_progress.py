"""Happy-path progress: print (N%) then exit 0."""
from __future__ import annotations

import argparse
import sys
import time


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--steps", type=int, default=5)
    p.add_argument("--sleep", type=float, default=0.2)
    args = p.parse_args()
    for i in range(args.steps + 1):
        pct = 100.0 * i / args.steps
        print(f"dummy-progress step {i}/{args.steps} ({pct:.1f}%)", flush=True)
        if i < args.steps:
            time.sleep(args.sleep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
