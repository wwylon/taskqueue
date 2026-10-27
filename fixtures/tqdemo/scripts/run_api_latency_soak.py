"""Run scripts/api_latency_soak.py from the taskqueue repo root (tqdemo cwd-safe)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent.parent.parent


def main() -> int:
    script = _REPO_ROOT / "scripts" / "api_latency_soak.py"
    if not script.is_file():
        print(f"missing {script}", file=sys.stderr)
        return 1
    return subprocess.call([sys.executable, str(script), *sys.argv[1:]], cwd=str(_REPO_ROOT))


if __name__ == "__main__":
    raise SystemExit(main())
