"""Shared client protocol golden fixtures (Python client + runner regex)."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

FIX = Path(__file__).resolve().parent / "fixtures" / "client_protocol" / "progress_lines.json"

# Mirror runner.py / log_issues progress + metric patterns (keep in sync).
_PROGRESS_RE = re.compile(r"\((\d+(?:\.\d+)?)%\)")
_TQ_METRIC_RE = re.compile(r"^TQ_METRIC\s+(\{.+\})\s*$")


def test_golden_fixture_loads() -> None:
    data = json.loads(FIX.read_text(encoding="utf-8"))
    assert data["exit_suspended"] == 75
    assert len(data["lines"]) >= 3


def test_python_client_progress_matches_golden(capsys: pytest.CaptureFixture[str]) -> None:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "clients" / "python"))
    import tq_client as tq  # noqa: E402

    data = json.loads(FIX.read_text(encoding="utf-8"))
    assert tq.EXIT_SUSPENDED == data["exit_suspended"]
    for item in data["lines"]:
        if item["kind"] != "progress":
            continue
        tq.progress(item["pct"], item.get("msg") or "")
        out = capsys.readouterr().out.strip()
        assert out == item["raw"]
        m = _PROGRESS_RE.search(out)
        assert m and float(m.group(1)) == item["pct"]


def test_metric_and_control_shapes() -> None:
    data = json.loads(FIX.read_text(encoding="utf-8"))
    for item in data["lines"]:
        if item["kind"] == "metric":
            m = _TQ_METRIC_RE.match(item["raw"])
            assert m
            assert json.loads(m.group(1)) == item["fields"]
        if item["kind"] == "control":
            assert item["file"]["action"] == "suspend"
            assert item["exit"] == 75
