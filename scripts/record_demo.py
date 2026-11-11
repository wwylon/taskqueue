"""Record docs/assets/demo.gif from the real web UI against synthetic demo data.

Runs the product on an isolated TQDAEMON_HOME + free port (never your live queue), drives it
with headless Chromium and writes the GIF. Re-records only when its inputs changed (see
``_inputs_hash``); ``--force`` ignores that, ``--check`` exits 1 when the GIF is stale.

    uv run --group browser python scripts/record_demo.py [--force | --check]
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GIF = ROOT / "docs" / "assets" / "demo.gif"
STAMP = GIF.with_suffix(".gif.sha")

# project -> [(task_id, steps, cpu, depends_on | None, fail_at)]
DEMO = {
    "ml-training": [
        ("train-model", 150, 1, None, 0),
        ("evaluate", 90, 1, "train-model", 0),
        ("export-onnx", 50, 1, "evaluate", 0),
    ],
    "data-pipeline": [
        ("ingest-logs", 120, 1, None, 0),
        ("clean-dedupe", 90, 1, "ingest-logs", 0),
        ("nightly-report", 30, 1, None, 6),
    ],
    "render-farm": [
        ("render-scene-01", 150, 1, None, 0),
        ("render-scene-02", 150, 1, None, 0),
        ("encode-video", 60, 1, "render-scene-02", 0),
    ],
}
TOUR = [("queue", 12), ("dashboard", 8), ("deps", 6), ("timeline", 8), ("queue", 5)]


def _inputs_hash() -> str:
    h = hashlib.sha256()
    files = sorted((ROOT / "tqdaemon" / "static").rglob("*")) + sorted((ROOT / "fixtures" / "demo").rglob("*.py")) + [Path(__file__)]
    for p in files:
        if p.is_file() and "__pycache__" not in p.parts:
            h.update(p.relative_to(ROOT).as_posix().encode())
            h.update(p.read_bytes().replace(b"\r\n", b"\n"))
    return h.hexdigest()


def _tq(env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "tqdaemon.cli", *args], env=env, cwd=ROOT, capture_output=True, text=True, timeout=300
    )


def _write_demo(tmp: Path) -> str:
    projects = "projects:\n"
    for name, tasks in DEMO.items():
        root = tmp / name
        q = root / "experiments" / "operator_queue"
        q.mkdir(parents=True)
        (root / "scripts").mkdir()
        shutil.copy(ROOT / "fixtures" / "demo" / "demo_task.py", root / "scripts" / "demo_task.py")
        projects += f"  {name}:\n    root: {root.as_posix()}\n"
        for tid, steps, cpu, dep, fail_at in tasks:
            extra = f', "--fail-at", "{fail_at}"' if fail_at else ""
            deps = f"depends_on: [{dep}]\n" if dep else ""
            (q / f"{tid}.yaml").write_text(
                f"task_id: {tid}\ndescription: {tid.replace('-', ' ')}\ncommand: python\n"
                f'args: ["scripts/demo_task.py", "--steps", "{steps}"{extra}]\n'
                f"wall_clock_cap_sec: 600\npriority: 50\nenabled: true\n{deps}"
                f"resources:\n  cpu_cores: {cpu}\n  ram_gib: 0.5\ndispatch: local\n",
                encoding="utf-8",
            )
    return projects


def _health(port: int) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=10) as r:
        return json.load(r)


def _record(port: int) -> list:
    from PIL import Image
    from playwright.sync_api import sync_playwright

    frames: list = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1200, "height": 675})
        pg.set_default_timeout(90_000)
        try:
            for page, n in TOUR:
                pg.goto(f"http://127.0.0.1:{port}/?page={page}", wait_until="domcontentloaded")
                # wait on the element, not a sleep (wait_for_function is blocked by the UI's CSP)
                pg.get_by_text("Loading...").first.wait_for(state="hidden")
                pg.wait_for_timeout(2500)
                for i in range(n):
                    im = Image.open(io.BytesIO(pg.screenshot())).convert("RGB").resize((960, 540), Image.LANCZOS)
                    frames.append((im, 1000 if i < n - 1 else 1400))
                    pg.wait_for_timeout(900)
        finally:
            b.close()
    return frames


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--check", action="store_true", help="exit 1 if demo.gif is stale; record nothing")
    args = ap.parse_args()
    want = _inputs_hash()
    current = GIF.is_file() and STAMP.is_file() and STAMP.read_text().strip() == want
    if args.check:
        print("demo.gif current" if current else "demo.gif STALE")
        return 0 if current else 1
    if current and not args.force:
        print("demo.gif current - nothing to record")
        return 0

    tmp = Path(tempfile.mkdtemp(prefix="tq-demo-"))
    home = tmp / "home"
    home.mkdir()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    (home / "projects.yaml").write_text(_write_demo(tmp), encoding="utf-8")
    env = {
        **os.environ,
        "TQDAEMON_HOME": str(home),
        "PYTHONPATH": str(ROOT / "clients" / "python") + os.pathsep + os.environ.get("PYTHONPATH", ""),
    }
    started = False
    try:
        # demo-only limits: the recording host is usually busy; this never touches the live queue
        for kv in (f"web_port={port}", "ram_limit_pct=97", "cpu_limit_pct=100", "scheduler_interval_sec=2"):
            _tq(env, "config", "set", kv)
        r = _tq(env, "start")
        started = True
        if r.returncode != 0:
            print(r.stdout, r.stderr)
            return 1
        deadline = time.time() + 300
        while time.time() < deadline:
            try:
                if _health(port)["counts"]["running"] >= 2:
                    break
            except (OSError, KeyError, ValueError):
                pass
            time.sleep(2)
        else:
            print("demo tasks never reached 2 running (host too busy?):")
            print(_tq(env, "list").stdout[-2000:])
            return 1
        frames = _record(port)
    finally:
        if started:
            _tq(env, "stop-daemon", "--force")
            time.sleep(3)
        shutil.rmtree(tmp, ignore_errors=True)

    q = [f.quantize(128, dither=0) for f, _ in frames]
    GIF.parent.mkdir(parents=True, exist_ok=True)
    q[0].save(GIF, save_all=True, append_images=q[1:], duration=[d for _, d in frames], loop=0, optimize=True)
    STAMP.write_text(want + "\n")
    size = GIF.stat().st_size / 1e6
    print(f"wrote {GIF} ({len(frames)} frames, {size:.2f} MB)")
    return 0 if size < 2.5 else 1


if __name__ == "__main__":
    raise SystemExit(main())
