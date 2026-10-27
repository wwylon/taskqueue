"""Check task: exit 0 if artifact content matches --expect, else fail."""
from __future__ import annotations

import argparse
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--path", default="artifacts/dummy-pipe-a.txt")
    p.add_argument("--expect", default="ok")
    args = p.parse_args()
    path = Path(args.path)
    if not path.is_file():
        print(f"(0.0%) missing artifact: {path}")
        return 1
    got = path.read_text(encoding="utf-8").strip()
    if got != args.expect:
        print(f"(0.0%) check failed: got {got!r} want {args.expect!r}")
        return 1
    print("(100.0%) check ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
