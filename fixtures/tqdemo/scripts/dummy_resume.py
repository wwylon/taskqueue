"""Checkpoint + resume_args demo. Exit 75 reserved for cooperative suspend (PR3)."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# Cooperative suspend exit code (wired in Client slice).
EXIT_SUSPENDED = 75


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
    if args.resume and ckpt.is_file():
        data = json.loads(ckpt.read_text(encoding="utf-8"))
        start = int(data.get("step", 0))
        print(f"resuming from step {start}", flush=True)
    elif args.resume:
        print("warning: --resume but no checkpoint; starting fresh", flush=True)

    for i in range(start, args.steps + 1):
        pct = 100.0 * i / args.steps
        print(f"dummy-resume step {i}/{args.steps} ({pct:.1f}%)", flush=True)
        ckpt.write_text(json.dumps({"step": i}), encoding="utf-8")
        if i < args.steps:
            time.sleep(args.sleep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
