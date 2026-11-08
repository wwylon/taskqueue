"""Checkpoint + resume_args demo using tq_client."""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT / "clients" / "python"))

from tq_client import (  # noqa: E402
    CheckpointCadence,
    load_checkpoint,
    maybe_suspend,
    progress,
    save_checkpoint,
)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out-dir", default="checkpoints/dummy-resume")
    p.add_argument("--steps", type=int, default=10)
    p.add_argument("--sleep", type=float, default=0.15)
    p.add_argument("--resume", action="store_true")
    args = p.parse_args()

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    ckpt = out / "checkpoint.json"

    start = 0
    if args.resume:
        state = load_checkpoint(ckpt)
        if state:
            start = int(state.get("step", 0))
            print(f"resuming from step {start}", flush=True)
        else:
            print("warning: --resume but no checkpoint; starting fresh", flush=True)

    cadence = CheckpointCadence(every_n_units=1, every_sec=600.0)
    for i in range(start, args.steps + 1):
        maybe_suspend()
        progress(100.0 * i / args.steps, f"dummy-resume step {i}/{args.steps}")
        if cadence.note_work():
            save_checkpoint(ckpt, {"step": i})
            cadence.reset()
        if i < args.steps:
            time.sleep(args.sleep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
