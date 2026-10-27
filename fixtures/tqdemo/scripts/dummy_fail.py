"""Exit nonzero after a short progress blip."""
from __future__ import annotations

import sys
import time


def main() -> int:
    print("dummy-fail starting (10.0%)", flush=True)
    time.sleep(0.1)
    print("dummy-fail exploding (20.0%)", flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
