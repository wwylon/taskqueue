"""Byte-tail log reads must stay O(window), not O(file size)."""

from __future__ import annotations

from pathlib import Path

from taskqueue.diagnose import _tail_log
from taskqueue.logio import (
    DEFAULT_LOG_TAIL_BYTES,
    MAX_LOG_TAIL_BYTES,
    clamp_tail_bytes,
    format_log_tail,
    read_log_tail,
    tail_log_lines,
)


def test_read_log_tail_small_file(tmp_path: Path) -> None:
    p = tmp_path / "small.log"
    p.write_bytes(b"a\nb\nc\n")
    tail = read_log_tail(p, max_bytes=1024)
    assert tail.truncated is False
    assert tail.file_size == p.stat().st_size
    assert tail.text == "a\nb\nc\n"
    assert "truncated" not in format_log_tail(tail)


def test_read_log_tail_truncates_and_aligns_newline(tmp_path: Path) -> None:
    p = tmp_path / "big.log"
    body = "".join(f"line-{i:04d}\n" for i in range(50))
    p.write_bytes(body.encode("utf-8"))
    size = p.stat().st_size
    tail = read_log_tail(p, max_bytes=80)
    assert tail.truncated is True
    assert tail.file_size == size
    assert tail.start_offset > 0
    assert not tail.text.startswith("e-")  # no mid-token from prior line
    assert "line-" in tail.text
    assert "line-0049" in tail.text.splitlines()[-1]
    formatted = format_log_tail(tail)
    assert formatted.startswith("--- truncated:")
    assert "line-" in formatted


def test_read_log_tail_does_not_use_read_text(tmp_path: Path, monkeypatch) -> None:
    p = tmp_path / "huge.log"
    chunk = b"x" * 1024 + b"\n"
    with open(p, "wb") as f:
        for _ in range(2048):  # ~2 MiB
            f.write(chunk)
        f.write(b"TAIL_MARKER\n")

    def boom(*_a, **_k):
        raise AssertionError("read_text must not be used for log tail")

    monkeypatch.setattr(Path, "read_text", boom)

    tail = read_log_tail(p, max_bytes=4096)
    assert tail.truncated is True
    assert "TAIL_MARKER" in tail.text
    assert len(tail.text.encode("utf-8")) <= 4096 + 64


def test_clamp_tail_bytes() -> None:
    assert clamp_tail_bytes(None) == DEFAULT_LOG_TAIL_BYTES
    assert clamp_tail_bytes(0) == DEFAULT_LOG_TAIL_BYTES
    assert clamp_tail_bytes(-1) == DEFAULT_LOG_TAIL_BYTES
    assert clamp_tail_bytes(100) == 100
    assert clamp_tail_bytes(MAX_LOG_TAIL_BYTES + 999) == MAX_LOG_TAIL_BYTES


def test_tail_log_lines_and_diagnose_helper(tmp_path: Path) -> None:
    p = tmp_path / "run.log"
    p.write_text("".join(f"L{i}\n" for i in range(200)), encoding="utf-8")
    lines = tail_log_lines(p, 5)
    assert lines == ["L195", "L196", "L197", "L198", "L199"]
    assert _tail_log(str(p), 3) == ["L197", "L198", "L199"]
    assert _tail_log(None) == []
    assert "missing" in _tail_log(str(tmp_path / "nope.log"))[0]


def test_tail_log_lines_huge_file_last_lines_only(tmp_path: Path) -> None:
    p = tmp_path / "huge.log"
    with open(p, "wb") as f:
        for i in range(100_000):
            f.write(f"row-{i}\n".encode())
    lines = tail_log_lines(p, 2, max_scan_bytes=64 * 1024)
    assert lines[-1] == "row-99999"
    assert lines[-2].startswith("row-")
