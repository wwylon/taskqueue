"""Behavior tests for views/timeline.js (Gantt SVG, stale handling, tick generation)."""
from __future__ import annotations

import json

from tests.test_js_app import run_js
from tests.test_js_smoke import EV

DATA = {
    "window_start": "2026-01-01T10:00:00", "span_sec": 3600, "bunch_end_at": "2026-01-01T10:40:00",
    "bars": [
        {"project": "p", "task_id": "t<1>", "status": "running", "kind": "running", "start_sec": 0, "end_sec": 1200,
         "start_at": "2026-01-01T10:00:00", "end_at": "2026-01-01T10:20:00", "duration_sec": 1200, "estimate_source": "history", "priority": 3},
        {"project": "p", "task_id": "t2", "status": "queued", "kind": "queued", "start_sec": 1200, "end_sec": 2400,
         "start_at": "2026-01-01T10:20:00", "end_at": "2026-01-01T10:40:00", "duration_sec": 1200, "estimate_source": "default", "priority": 0, "clipped": True},
        {"task_id": "nightly", "kind": "cron", "start_sec": 3000, "end_sec": 3600, "duration_sec": 600},
    ],
}


def test_load_renders_svg_rows_labels_clicks_and_bunch_banner():
    out = run_js(
        EV
        + f"""
const {{ g, doc }} = await setup({{ app: true, routes: {{ '/api/timeline': {json.dumps(DATA)} }} }});
await g.flush();
const r = {{}};
doc.getElementById('tl-hours').value = '2';
g.fetches.length = 0;
const ok = await g._loadTimelineNow();
const host = doc.getElementById('tl-scroll');
r.ok = ok;
r.url = g.fetches[0].url;
r.svg = host.innerHTML.includes('<svg class="tl-svg"');
r.rows = (host.innerHTML.match(/<title>/g) || []).length;
r.escaped = host.innerHTML.includes('p/t&lt;1&gt;');
r.estimated = host.innerHTML.includes('~');                     // non-history estimate gets a tilde
r.clickable = (host.innerHTML.match(/tqOpenDrawer/g) || []).length > 0;
r.cronNoClick = !host.innerHTML.includes('"nightly"') ;
r.colors = ['#9184d9', '#60a5fa', '#34d399'].map(c => host.innerHTML.includes(c));
r.bunch = [doc.getElementById('tl-bunch').textContent.length > 0, !!doc.getElementById('tl-bunch').hidden];
r.stale = !!doc.getElementById('tl-stale').hidden;
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert out["ok"] is True
    assert out["url"].startswith("/api/timeline") and "hours=2" in out["url"]
    assert out["svg"] is True and out["rows"] == 3 and out["escaped"] is True and out["estimated"] is True
    assert out["clickable"] is True
    assert out["colors"] == [True, True, True]
    assert out["bunch"][0] is True and out["bunch"][1] is False


def test_failed_poll_keeps_last_chart_and_marks_stale_then_empty_state():
    out = run_js(
        EV
        + f"""
let mode = 'ok';
const {{ g, doc }} = await setup({{ app: true, routes: {{ '/api/timeline': () => (mode === 'ok' ? {json.dumps(DATA)} : mode === 'empty' ? {{ bars: [], window_start: '2026-01-01T10:00:00' }} : {{ error: 'x' }}) }} }});
await g.flush();
const r = {{}};
const host = doc.getElementById('tl-scroll');
await g._loadTimelineNow();
const before = host.innerHTML;
mode = 'bad';
r.ok = await g._loadTimelineNow();
r.kept = host.innerHTML === before;
r.stale = [doc.getElementById('tl-stale').hidden, doc.getElementById('tl-stale').textContent];
mode = 'empty';
r.okEmpty = await g._loadTimelineNow();
r.cleared = host.innerHTML === '';
r.staleGone = !doc.getElementById('tl-stale').hidden;
r.bunchHidden = doc.getElementById('tl-bunch').textContent === '';
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert out["ok"] is False and out["kept"] is True
    assert out["stale"][0] != out["staleGone"] or out["stale"][1]
    assert out["okEmpty"] is True and out["cleared"] is True and out["bunchHidden"] is True


def test_time_helpers_and_tick_steps_scale_with_span():
    out = run_js(
        EV
        + """
const { g } = await setup({ app: true });
await g.flush();
const r = {};
const mid = new Date(2026, 0, 1, 0, 0, 0).getTime();
const stepMin = span => { const t = g._tlTicks(mid, span); return t.length > 1 ? (t[1] - t[0]) / 60000 : null; };
r.steps = [1, 4, 12, 48, 200].map(h => stepMin(h * 3600));
r.first = new Date(g._tlTicks(mid + 10 * 60000, 3600)[0]).getMinutes();   // aligned to the 15-minute grid
const day = new Date(2026, 0, 1, 10, 0).toISOString();
r.same = g._tlAt(new Date(2026, 0, 1, 10, 5).toISOString(), day) === g._tlClock(new Date(2026, 0, 1, 10, 5));
r.other = g._tlAt(new Date(2026, 0, 2, 10, 5).toISOString(), day).includes(' ');
r.bad = g._tlAt('garbage', day);
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert out["steps"] == [15, 30, 120, 360, 1440]
    assert out["first"] == 15
    assert out["same"] is True and out["other"] is True and out["bad"] == ""


def test_load_timeline_auto_is_rate_limited_but_manual_is_forced():
    out = run_js(
        EV
        + f"""
const {{ g }} = await setup({{ app: true, routes: {{ '/api/timeline': {json.dumps(DATA)} }} }});
await g.flush();
const n = () => g.fetches.filter(f => f.url.startsWith('/api/timeline')).length;
const r = {{}};
const base = n();
await g.loadTimeline(false); await g.flush();
const afterManual = n();
await g.loadTimeline(true); await g.flush();         // within minGap of the manual load: skipped
r.manual = afterManual - base;
r.autoSkipped = n() === afterManual;
await g.loadTimeline(false); await g.flush();
r.forcedAgain = n() === afterManual + 1;
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert out["manual"] >= 1 and out["autoSkipped"] is True and out["forcedAgain"] is True
