"""Run a pytest file as a tqdemo task with (N%) progress lines.

Usage:
    python scripts/run_pytest.py tests/test_stress.py
    python scripts/run_pytest.py tests/test_scale.py

The test path is resolved relative to the repo root (two levels above this
script: fixtures/tqdemo/scripts/ -> fixtures/tqdemo/ -> fixtures/ -> repo/).
This means the task cwd (the tqdemo project root) doesn't matter.

Runs pytest as a subprocess; emits one (N%) line per passing/failing/skipping
test so the daemon progress bar fills. Exits with pytest's exit code.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

# scripts/ -> tqdemo/ -> fixtures/ -> repo root
_REPO_ROOT = Path(__file__).parent.parent.parent.parent


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: run_pytest.py <test_file>", file=sys.stderr)
        return 1

    test_file = _REPO_ROOT / sys.argv[1]
    if not test_file.exists():
        print(f"test file not found: {test_file}", file=sys.stderr)
        return 1

    # Collect test count first (fast, no output)
    collect = subprocess.run(
        [sys.executable, "-m", "pytest", str(test_file), "--collect-only", "-q"],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
    )
    total_match = re.search(r"(\d+) tests? collected", collect.stdout + collect.stderr)
    total = int(total_match.group(1)) if total_match else 0

    print(f"running {total} tests from {sys.argv[1]} (0.0%)", flush=True)

    # Run tests with -v so each result appears on its own line
    proc = subprocess.Popen(
        [sys.executable, "-m", "pytest", str(test_file), "-v", "--tb=short", "--no-header"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        cwd=_REPO_ROOT,
    )

    done = 0
    # Match lines like: "tests/test_stress.py::test_foo PASSED" or FAILED/SKIPPED/ERROR
    result_re = re.compile(r"::(test_\w+)\s+(PASSED|FAILED|SKIPPED|ERROR|XFAIL|XPASS)")

    for line in proc.stdout:
        print(line, end="", flush=True)
        if result_re.search(line) and total > 0:
            done += 1
            pct = 100.0 * done / total
            print(f"({pct:.1f}%)", flush=True)

    proc.wait()
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
