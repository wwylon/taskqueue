"""Produce a tiny artifact for the pipe check demo."""
from __future__ import annotations

import argparse
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="artifacts/dummy-pipe-a.txt")
    p.add_argument("--value", default="ok")
    args = p.parse_args()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(args.value + "\n", encoding="utf-8")
    print("(100.0%) wrote", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
