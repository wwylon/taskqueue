"""Behavior tests for components/sparks.js (gauge sparklines and the run-metrics canvas chart)."""
from __future__ import annotations

from tests.test_js_app import run_js
from tests.test_js_smoke import EV


def test_scale_units_formats_and_flat_series_get_a_floor():
    out = run_js(
        EV
        + """
const { g } = await setup({ app: true });
await g.flush();
const r = {};
const flat = g._sparkScale([{ v: 5 }, { v: 5 }]);
r.flat = [flat.hi > flat.lo, (flat.lo + flat.hi) / 2];
const wide = g._sparkScale([{ v: 0 }, { v: 10 }]);
r.wide = [wide.lo < 0, wide.hi > 10];
r.units = ['ram', 'gpu', 'daemon', 'cpu'].map(g._sparkUnit);
r.fmt = [g._sparkFmt('ram', 1.234), g._sparkFmt('cpu', 3.6)];
const pts = g._sparkPoints([{ v: 0 }, { v: 5 }, { v: 10 }], { lo: 0, hi: 10 });
r.pts = [pts[0].x, pts[2].x, pts[0].y > pts[2].y];
r.single = g._sparkPoints([{ v: 1 }], { lo: 0, hi: 2 })[0].x;
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert out["flat"][0] is True and out["flat"][1] == 5
    assert out["wide"] == [True, True]
    assert out["units"] == [" GiB", " GiB", " MB", ""]
    assert out["fmt"] == ["1.2", "4"]
    assert out["pts"] == [1, 99, True] and out["single"] == 50


def test_record_samples_throttle_persist_and_caption():
    out = run_js(
        EV
        + """
const { g, S } = await setup({ app: true });
await g.flush();
g._computeGaugeModel = () => ({ cpuUsed: 2, gpuUsed: 1.5, ramUsed: 8 });
S.status = { running: [{}, {}], queued_count: 3, daemon_rss_mb: 120 };
const r = {};
const store = () => JSON.parse(g.sessionStorage.getItem('tq_spark_hist') || 'null');
g.sessionStorage.removeItem('tq_spark_hist');
g.tqRecordSparks();
const first = store();
r.first = first && [first.running.length, first.running[0].v, first.queued[0].v, first.cpu[0].v, first.daemon[0].v];
g.tqRecordSparks();                                    // inside the 10s window: no new sample
r.throttled = store().cpu.length;
r.caption1 = g._sparkCaption('ram');
r.empty = g._sparkCaption('nope');
S.status = null;
g.tqRecordSparks();                                    // no status: nothing happens
r.noStatus = store().cpu.length;
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert out["first"] == [1, 2, 3, 2, 120]
    assert out["throttled"] == 1
    assert out["caption1"] == "8.0 GiB"
    assert out["empty"] == "" and out["noStatus"] == 1


def test_saved_history_is_restored_and_stale_or_bad_samples_dropped():
    out = run_js(
        EV
        + """
const { g, S } = await setup({ app: true });
await g.flush();
const now = Date.now();
g.sessionStorage.setItem('tq_spark_hist', JSON.stringify({
  ram: [{ t: now - 3600e3, v: 1 }, { t: now - 120e3, v: 4 }, { t: now - 1e3, v: 6 }, { t: 'x', v: 1 }, null],
  cpu: 'bogus',
}));
const r = {};
const { pathToFileURL } = await import('node:url');
const fresh = await import(pathToFileURL(process.cwd() + '/taskqueue/static/js/components/sparks.js').href + '?fresh');   // module-local state starts unloaded
g._computeGaugeModel = () => ({ cpuUsed: 1, gpuUsed: 0, ramUsed: 7 });
S.status = { running: [], queued_count: 0, daemon_rss_mb: 1 };
fresh.tqRecordSparks();                                 // loads saved history, then appends a live sample
r.caption = fresh._sparkCaption('ram');
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert out["caption"] == "4.0 → 7.0 GiB · 2m"  # stale (1h old) and malformed samples dropped, 4 and 6 kept, 7 appended; 3 points span 2 min


def test_draw_sparks_fills_svg_and_caption_hosts():
    out = run_js(
        EV
        + """
const { g, doc, S } = await setup({ app: true });
await g.flush();
g._computeGaugeModel = () => ({ cpuUsed: 2, gpuUsed: 1, ramUsed: 8 });
S.status = { running: [], queued_count: 0, daemon_rss_mb: 10 };
const svg = doc.createElement('svg'); svg.setAttribute('data-spark', 'cpu ram');
const cap = doc.createElement('span'); cap.setAttribute('data-spark-cap', 'ram');
doc.body.appendChild(svg); doc.body.appendChild(cap);
g.sessionStorage.removeItem('tq_spark_hist');
g.tqRecordSparks();
const r = { svg: svg.innerHTML.length > 0, cap: cap.textContent };
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert out["svg"] is True and out["cap"] == "8.0 GiB"


def test_canvas_helper_hides_for_short_history_and_paints_otherwise():
    out = run_js(
        EV
        + """
const samples = n => Array.from({ length: n }, (_, i) => ({ ts: i, cpu_pct: i, rss_mb: 10 + i }));
const { g, doc } = await setup({ app: true, routes: { '/api/runs/1/history': () => samples(1), '/api/runs/2/history': () => ({ samples: samples(3) }) } });
await g.flush();
const mk = () => { const c = doc.createElement('canvas'); c.getContext = () => new Proxy({}, { get: (_t, k) => (k === 'measureText' ? () => ({ width: 10 }) : () => {}), set: () => true }); c.getBoundingClientRect = () => ({ width: 400, height: 150 }); return c; };
const r = {};
const a = mk(); await g._drawSparklineOnCanvas(a, 1);
r.short = a.style.display;
const b = mk(); await g._drawSparklineOnCanvas(b, 2);
r.long = b.style.display;
await g._drawSparklineOnCanvas(null, 1);               // no canvas: no-op
r.legacy = (await g._drawSparkline(1)) === undefined;
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert out["short"] == "none" and out["long"] == "block" and out["legacy"] is True
