"""Poll TQ_CONTROL_FILE; exit 75 on suspend. Exercises cooperative client contract."""
from __future__ import annotations

import sys
import time
from pathlib import Path

# Repo clients/python on path
_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT / "clients" / "python"))

from tq_client import EXIT_SUSPENDED, maybe_suspend, progress, save_checkpoint  # noqa: E402


def main() -> int:
    ckpt = Path("checkpoints/dummy-suspend/checkpoint.json")
    for i in range(0, 101, 5):
        maybe_suspend()
        progress(float(i), f"dummy-suspend step {i}")
        save_checkpoint(ckpt, {"step": i})
        time.sleep(0.3)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
