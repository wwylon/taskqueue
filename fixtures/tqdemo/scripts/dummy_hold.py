"""Short hold for exclusive / deps / cap fixtures."""
from __future__ import annotations

import argparse
import time


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--seconds", type=float, default=2.0)
    p.add_argument("--label", default="hold")
    args = p.parse_args()
    steps = max(1, int(args.seconds / 0.25))
    for i in range(steps + 1):
        pct = 100.0 * i / steps
        print(f"{args.label} ({pct:.1f}%)", flush=True)
        if i < steps:
            time.sleep(args.seconds / steps)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
