"""Synthetic demo job for scripts/record_demo.py: reports progress via tq_client."""
from __future__ import annotations

import argparse
import sys
import time

from tq_client import progress


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--steps", type=int, default=60)
    p.add_argument("--fail-at", type=int, default=0)
    args = p.parse_args()
    for i in range(args.steps + 1):
        progress(100.0 * i / args.steps, f"step {i}/{args.steps}")
        if args.fail_at and i == args.fail_at:
            print("demo: simulated failure", file=sys.stderr)
            return 1
        time.sleep(1.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
