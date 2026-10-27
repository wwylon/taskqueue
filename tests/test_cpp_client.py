"""C++ client header: compiles, prints regex-friendly progress, rejects temporaries in progress_range."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

HDR = Path(__file__).resolve().parents[1] / "clients" / "cpp"
GXX = shutil.which("g++") or shutil.which("clang++")
pytestmark = [pytest.mark.skipif(GXX is None, reason="no C++ compiler"), pytest.mark.slow]
PROGRESS_RE = re.compile(r"\((\d+(?:\.\d+)?)%\)")


def _compile(tmp_path, src):
    cpp = tmp_path / "t.cpp"
    cpp.write_text(src, encoding="utf-8")
    exe = tmp_path / "t.exe"
    return subprocess.run([GXX, "-std=c++17", f"-I{HDR}", str(cpp), "-o", str(exe)],
                          capture_output=True, text=True, timeout=120), exe


def test_progress_is_fixed_point_and_matches_daemon_regex(tmp_path):
    res, exe = _compile(tmp_path, '#include "tq_client.hpp"\n'
                        'int main(){ tq::progress(0.00001, "a"); tq::progress(150); '
                        'std::vector<int> v{1,2}; for (int x : tq::progress_range(v)) (void)x; }\n')
    assert res.returncode == 0, res.stderr
    out = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30).stdout
    assert "e-" not in out
    lines = out.splitlines()
    assert lines[0] == "a (0.00%)" and lines[1] == "(100.00%)"
    assert all(PROGRESS_RE.search(ln) for ln in lines)


def test_progress_range_rejects_temporaries(tmp_path):
    res, _ = _compile(tmp_path, '#include "tq_client.hpp"\n'
                      'int main(){ for (int x : tq::progress_range(std::vector<int>{1})) (void)x; }\n')
    assert res.returncode != 0


def test_metric_line_is_parsed_by_the_runner(tmp_path):
    import json

    from taskqueue.runner import _TQ_METRIC_RE

    res, exe = _compile(tmp_path, '#include "tq_client.hpp"\n'
                        'int main(){ tq::metric({{"loss", 0.125}, {"ep\\"och", 3}}); }\n')
    assert res.returncode == 0, res.stderr
    out = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30).stdout.strip()
    m = _TQ_METRIC_RE.match(out)
    assert m and json.loads(m.group(1)) == {"loss": 0.125, 'ep"och': 3}
