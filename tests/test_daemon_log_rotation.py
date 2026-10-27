"""daemon.log stays bounded while the daemon runs; heartbeat lines are throttled."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_rotate_own_log_copies_then_truncates_open_stdout(tmp_path: Path) -> None:
    code = (
        "import sys; sys.path.insert(0, r'%s')\n"
        "from pathlib import Path\n"
        "from taskqueue.daemon import rotate_own_log\n"
        "print('x' * 3000, flush=True)\n"
        "print('rotated' if rotate_own_log(Path(r'%s'), max_bytes=1000) else 'no', flush=True)\n"
        "print('after', flush=True)\n"
    ) % (ROOT, tmp_path)
    log = tmp_path / "daemon.log"
    with open(log, "a", encoding="utf-8") as fh:
        subprocess.run([sys.executable, "-c", code], stdout=fh, stderr=subprocess.STDOUT, timeout=120, check=True)
    old = (tmp_path / "daemon.log.1").read_text(encoding="utf-8")
    new = log.read_text(encoding="utf-8")
    assert "x" * 3000 in old
    assert "x" * 100 not in new and "rotated" in new and "after" in new
    assert log.stat().st_size < 1000


def test_rotate_ignores_a_log_that_is_not_stdout(tmp_path: Path) -> None:
    from taskqueue.daemon import rotate_own_log

    (tmp_path / "daemon.log").write_text("y" * 5000, encoding="utf-8")
    assert rotate_own_log(tmp_path, max_bytes=10) is False
    assert (tmp_path / "daemon.log").stat().st_size == 5000
