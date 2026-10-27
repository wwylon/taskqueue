"""Optional capped RAM alloc — manual / non-default CI only."""
from __future__ import annotations

import argparse
import time


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--mib", type=int, default=64)
    p.add_argument("--hold-sec", type=float, default=1.0)
    args = p.parse_args()
    # Cap hard so accidents stay cheap.
    mib = min(max(args.mib, 1), 256)
    print(f"allocating ~{mib} MiB (0.0%)", flush=True)
    buf = bytearray(mib * 1024 * 1024)
    buf[0] = 1
    buf[-1] = 2
    print(f"holding ({50.0:.1f}%)", flush=True)
    time.sleep(args.hold_sec)
    print(f"releasing ({100.0:.1f}%)", flush=True)
    del buf
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
