"""Print one progress line then hang (freeze-detection fixture)."""
from __future__ import annotations

import argparse
import time


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--hang-sec", type=float, default=3600)
    args = p.parse_args()
    print("dummy-frozen starting (1.0%)", flush=True)
    time.sleep(args.hang_sec)
    print("dummy-frozen done (100.0%)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
