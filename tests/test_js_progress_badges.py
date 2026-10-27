"""Stalled chip and negative-result badge."""
from __future__ import annotations

import json
from pathlib import Path

from tests.test_js_modules import _run

JS = Path(__file__).resolve().parent.parent / "taskqueue" / "static" / "js"


def test_progress_health_chip_and_negative_badge():
    out = _run(
        f"globalThis.i18n = {{ t: (k, v) => k + (v ? JSON.stringify(v) : '') }};\n"
        f"import {{ progressHealthHtml }} from {json.dumps((JS / 'util' / 'format.js').as_uri())};\n"
        f"import {{ badge }} from {json.dumps((JS / 'util' / 'labels.js').as_uri())};\n"
        "console.log(JSON.stringify({ stalled: progressHealthHtml({ age_sec: 900, stalled: true }),"
        " recent: progressHealthHtml({ age_sec: 20, stalled: false }), age: progressHealthHtml({ age_sec: 300, stalled: false }),"
        " none: progressHealthHtml(null), neg: badge('completed', 'tq.run.negative'), ok: badge('completed') }));"
    )
    assert "tq-stall" in out["stalled"] and "progress.stalled" in out["stalled"]
    assert out["recent"] == "" and out["none"] == "" and "tq-progress-age" in out["age"]
    assert "badge-negative" in out["neg"] and "status.negative" in out["neg"]
    assert "badge-negative" not in out["ok"]
