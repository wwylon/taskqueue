"""Lie about progress without doing real work (progress-sanity fixture)."""
from __future__ import annotations

import time


def main() -> int:
    for pct in (10.0, 90.0, 50.0, 99.0):
        print(f"lying ({pct:.1f}%)", flush=True)
        time.sleep(0.1)
    print("done without earning it (100.0%)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
