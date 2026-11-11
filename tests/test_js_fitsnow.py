"""Dashboard "What fits now" strip (components/fitsnow.js) against /api/plan."""

from __future__ import annotations

import json

from tests.test_js_app import run_js
from tests.test_js_modules import JS, _run

PLAN = {
    "headroom": {"cpu_cores": 6.04, "ram_gib": 40, "gpu_vram_gib": 0},
    "would_start": 2,
    "tasks": [
        {"project": "p", "task_id": "big", "verdict": "blocked", "reason": "RAM: need 90 GiB"},
        {"project": "p", "task_id": "small", "verdict": "start", "reason": ""},
    ],
}


def test_fits_now_html_free_next_blocked_and_empty():
    mod = json.dumps((JS / "components" / "fitsnow.js").as_uri())
    out = _run(
        "globalThis.window = globalThis;"
        "globalThis.i18n = { t: (k, v) => k + (v ? JSON.stringify(v) : '') };"
        f"const f = await import({mod});"
        f"const plan = {json.dumps(PLAN)};"
        "const blocked = { headroom: { cpu_cores: 0, ram_gib: 1, gpu_vram_gib: 8 }, tasks: [plan.tasks[0]] };"
        "console.log(JSON.stringify([f._fitsNowHtml(plan), f._fitsNowHtml(blocked),"
        " f._fitsNowHtml({ headroom: plan.headroom, tasks: [] }), f._fitsNowHtml({})]));"
    )
    start, blocked, empty, none = out
    assert "fits.cpuCores{&quot;n&quot;:&quot;6&quot;} · RAM 40 GiB" in start and "VRAM" not in start  # no GPU headroom → no VRAM part
    assert "dashboard.fitsNext" in start and "p/small" in start and "&quot;n&quot;:2" in start
    assert "VRAM 8 GiB" in blocked and "dashboard.fitsBlocked" in blocked and "RAM: need 90 GiB" in blocked
    assert "dashboard.fitsNothingQueued" in empty
    assert none == ""


def test_fits_now_strip_fetches_on_dashboard_and_throttles():
    out = run_js(
        f"""
const {{ g, doc, S }} = await setup({{ app: true, url: 'http://localhost:8765/?page=dashboard', routes: {{ '/api/plan': {json.dumps(PLAN)} }} }});
await g.flush();
const plans = () => g.fetches.filter(f => f.url.startsWith('/api/plan')).length;
const strip = doc.getElementById('fits-now');
const r = {{}};
S.fitsNowAt = 0;
await g.tqRefreshFitsNow(true);
r.first = [plans() >= 1, strip.hidden, strip.innerHTML.includes('dashboard.fitsNext')];
const n = plans();
await g.tqRefreshFitsNow();                         // within 15 s: throttled
r.throttled = plans() === n;
await g.tqRefreshFitsNow(true);                     // forced (structural change)
r.forced = plans() === n + 1;
doc.getElementById('dashboard').classList.remove('active');
await g.tqRefreshFitsNow(true);                     // another page is showing: no dry run
r.offPage = plans() === n + 1;
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert out == {"first": [True, False, True], "throttled": True, "forced": True, "offPage": True}
