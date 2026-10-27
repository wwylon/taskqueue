"""Generate a gigantic log to stress-test the log viewer.

Streams ~120 MB of structured text:
  - phase 1: 500 000 short lines  (~50 MB)  — tests line-count handling
  - phase 2: 200 lines of 256 KB each       (~50 MB)  — tests wide-line handling
  - phase 3: 20 MB of rapid-fire JSON blobs — tests binary-ish content

Emits (N%) progress so the dashboard bar fills.
"""

from __future__ import annotations

import json
import sys
import time


def _pct(done: int, total: int) -> None:
    print(f"({100.0 * done / total:.1f}%)", flush=True)


def main() -> int:
    phases = 3
    total_phases = phases

    # --- Phase 1: many short lines ---
    print("=== phase 1/3: 500 000 short lines ===", flush=True)
    n = 500_000
    t0 = time.monotonic()
    for i in range(n):
        print(f"line {i:07d}  ts={time.monotonic()-t0:.4f}  payload=stress-test-short-line-content", flush=True)
        if (i + 1) % 50_000 == 0:
            _pct(i + 1, n * 3)   # phase 1 is 1/3 of total
    _pct(n, n * 3)

    # --- Phase 2: very long lines ---
    print("\n=== phase 2/3: 200 lines × 256 KB ===", flush=True)
    chunk = "X" * (256 * 1024 - 60)
    for i in range(200):
        print(f"wide-line {i:03d} [{chunk}]", flush=True)
        if (i + 1) % 20 == 0:
            _pct(n + (i + 1) * (n // 200), n * 3)
    _pct(n * 2, n * 3)

    # --- Phase 3: rapid JSON blobs ---
    print("\n=== phase 3/3: 10 000 JSON blobs (2 KB each) ===", flush=True)
    blob_count = 10_000
    blob = {"event": "stress", "data": "Y" * 1900, "tags": ["a", "b", "c"]}
    for i in range(blob_count):
        print(json.dumps({**blob, "seq": i}), flush=True)
        if (i + 1) % 1_000 == 0:
            _pct(n * 2 + (i + 1) * (n // blob_count), n * 3)
    _pct(n * 3, n * 3)

    elapsed = time.monotonic() - t0
    print(f"\ndone — streamed ~120 MB in {elapsed:.1f}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
