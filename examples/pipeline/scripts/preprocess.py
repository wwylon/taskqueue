"""Pipeline step 1 — write a tiny artifact."""
from __future__ import annotations

import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT / "clients" / "python"))

from tq_client import progress  # noqa: E402


def main() -> int:
    out = Path("runs/pipeline/data.txt")
    out.parent.mkdir(parents=True, exist_ok=True)
    for i in range(0, 5):
        progress(100.0 * (i + 1) / 5, f"preprocess {i + 1}/5")
        time.sleep(0.05)
    out.write_text("ready\n", encoding="utf-8")
    progress(100.0, "preprocess done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
