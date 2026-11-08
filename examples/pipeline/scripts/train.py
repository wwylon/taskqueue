"""Pipeline step 2 — depends on preprocess artifact."""
from __future__ import annotations

import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT / "clients" / "python"))

from tq_client import progress  # noqa: E402


def main() -> int:
    data = Path("runs/pipeline/data.txt")
    if not data.is_file():
        print(f"missing artifact: {data}", flush=True)
        return 2
    for i in range(0, 5):
        progress(100.0 * (i + 1) / 5, f"train {i + 1}/5")
        time.sleep(0.05)
    progress(100.0, "train done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
