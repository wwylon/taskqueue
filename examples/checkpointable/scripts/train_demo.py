"""Checkpointable demo using tq_client (cadence + suspend + resume)."""
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
    metric,
    progress,
    save_checkpoint,
)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="runs/train_demo")
    p.add_argument("--steps", type=int, default=20)
    p.add_argument("--sleep", type=float, default=0.05)
    p.add_argument("--resume", action="store_true")
    args = p.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    ckpt = out / "checkpoint.json"

    start = 0
    if args.resume:
        state = load_checkpoint(ckpt) or {}
        start = int(state.get("step", 0))
        print(f"resuming from step {start}", flush=True)

    cadence = CheckpointCadence(every_n_units=5, every_sec=600.0)
    for step in range(start, args.steps):
        maybe_suspend()
        progress(100.0 * (step + 1) / args.steps, f"train-demo step {step + 1}")
        metric(step=step + 1)
        if cadence.note_work():
            save_checkpoint(ckpt, {"step": step + 1})
            cadence.reset()
        time.sleep(args.sleep)
    save_checkpoint(ckpt, {"step": args.steps})
    progress(100.0, "done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
