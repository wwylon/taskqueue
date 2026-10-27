"""Behavior tests for components/logdock.js (bottom log dock: static load, live SSE tail, close/clear)."""
from __future__ import annotations

from tests.test_js_app import run_js
from tests.test_js_smoke import EV

PRE = """
const { g, doc, S } = await setup({ app: true, routes: {} });
await g.flush();
const r = {};
const frame = async () => { g.fireTimers(0); await g.flush(); };  // log writes are batched per animation frame
const mk = () => {
  const panel = doc.getElementById('tq-log-panel') || doc.add('tq-log-panel'); panel.hidden = true;
  const body = doc.getElementById('tq-log-body') || doc.add('tq-log-body', { parent: panel });
  const title = doc.getElementById('tq-log-title') || doc.add('tq-log-title', { parent: panel });
  const live = doc.getElementById('tq-log-live') || doc.add('tq-log-live', { parent: panel });
  return { panel, body, title, live };
};
"""


def _js(body: str) -> str:
    return EV + PRE + body + "\nconsole.log('RESULT:' + JSON.stringify(r));\n"


def test_missing_dock_elements_are_noops():
    out = run_js(_js("""
g.tqAppendLogText('x'); g.tqClearLogView(); g.tqCloseLog();
g.tqOpenLogPanel(1, 'p', 't', false);
await g.tqLoadStaticLog(1);
r.ok = true;
"""))
    assert out["ok"] is True


def test_open_static_log_loads_tail_and_sets_title_and_live_flag():
    out = run_js(_js("""
const e = mk();
const text = Array.from({ length: 300 }, (_, i) => 'line' + i).join('\\n');
g.fetch = async url => { r.url = url; return { ok: true, text: async () => text }; };
g.tqOpenLogPanel(7, 'proj', 'task', false);
await g.flush(); await frame();
r.open = [e.panel.hidden, e.title.textContent, e.live.hidden, doc.body.classList.has('tq-log-open')];
r.lines = [e.body.children.length, e.body.innerHTML.includes('line299'), e.body.innerHTML.includes('line99<')];
g.tqClearLogView(); r.cleared = e.body.innerHTML;
g.tqCloseLog();
r.closed = [e.panel.hidden, doc.body.classList.has('tq-log-open'), e.live.hidden];
"""))
    assert out["url"] == "/api/logs/7"
    assert out["open"] == [False, "proj/task #7", True, True]
    assert out["lines"][0] <= 200 and out["lines"][1] is True and out["lines"][2] is False
    assert out["cleared"] == "" and out["closed"] == [True, False, True]


def test_append_escapes_html_and_caps_at_200_children():
    out = run_js(_js("""
const e = mk();
g.tqAppendLogText('<b>x</b>\\n'); await frame();
r.esc = e.body.innerHTML.includes('&lt;b&gt;x&lt;/b&gt;') && !e.body.innerHTML.includes('<b>');
const kids = Array.from({ length: 205 }, (_, i) => ({ i }));
const fake = { children: kids, insertAdjacentHTML() {}, removeChild(c) { kids.splice(kids.indexOf(c), 1); }, get firstChild() { return kids[0]; }, scrollHeight: 9, scrollTop: 0 };
const realGet = doc.getElementById.bind(doc);
doc.getElementById = id => (id === 'tq-log-body' ? fake : realGet(id));
g.tqAppendLogText('z'); await frame();
r.count = kids.length; r.first = kids[0].i; r.scrolled = fake.scrollTop;
"""))
    assert out["esc"] is True and out["count"] == 200 and out["first"] == 5 and out["scrolled"] == 9


def test_live_stream_appends_replaces_previous_and_falls_back_on_error():
    out = run_js(_js("""
const e = mk();
const made = [];
g.EventSource = function (u) { this.url = u; this.closed = false; this.close = () => { this.closed = true; }; made.push(this); };
g.fetch = async url => { r.fetched = url; return { ok: true, text: async () => 'saved' }; };
g.tqOpenLogPanel(5, 'p', 't', true);
const es1 = made[made.length - 1];
r.live = [e.live.hidden, es1.url.includes('/api/logs/5/stream')];
es1.onmessage({ data: 'hello' }); await frame();
r.got = e.body.innerHTML.includes('hello');
g.tqOpenLogPanel(6, 'p', 't', true);
const es2 = made[made.length - 1];
r.replaced = [es1.closed, es2 !== es1];
es1.onmessage({ data: 'stale' }); await frame();               // old stream is ignored
r.stale = e.body.innerHTML.includes('stale');
es2.onerror();                                                  // nothing received yet -> static fallback
await g.flush(); await frame();
r.fallback = [es2.closed, e.live.hidden, r.fetched, e.body.innerHTML.includes('saved')];
g.tqOpenLogPanel(8, 'p', 't', true);
const es3 = made[made.length - 1];
g.tqCloseLog();
r.closeStops = es3.closed;
"""))
    assert out["live"] == [False, True] and out["got"] is True
    assert out["replaced"] == [True, True] and out["stale"] is False
    assert out["fallback"] == [True, True, "/api/logs/6", True]
    assert out["closeStops"] is True


def test_static_load_error_is_rendered_in_the_dock():
    out = run_js(_js("""
const e = mk();
g.fetch = async () => { throw new Error('offline'); };
await g.tqLoadStaticLog(3);
r.html = e.body.innerHTML.includes('offline');
"""))
    assert out["html"] is True


def test_static_load_drops_a_response_that_arrives_after_a_newer_request():
    out = run_js(_js("""
const e = mk();
const pending = [];
g.fetch = url => new Promise(res => pending.push(t => res({ ok: true, text: async () => t })));
const older = g.tqLoadStaticLog(1); const newer = g.tqLoadStaticLog(2);
pending[1]('NEW'); await newer; await frame();
pending[0]('OLD'); await older; await frame();
r.html = e.body.innerHTML;
"""))
    assert "NEW" in out["html"] and "OLD" not in out["html"]


def test_filter_level_wrap_and_regex_fallback():
    out = run_js(_js(r"""
const e = mk();
const line = t => ({ textContent: t, hidden: false, classList: { contains: c => c === 'tq-log-line' } });
const kids = [line('[INFO] epoch 1'), line('[ERROR] boom'), line('[WARN] slow')];
const fake = { children: kids, classList: e.body.classList };
const realGet = doc.getElementById.bind(doc);
doc.getElementById = id => (id === 'tq-log-body' ? fake : realGet(id));
g.tqSetLogFilter({ value: 'boo|slo' });
r.filtered = kids.map(c => c.hidden);
g.tqSetLogFilter({ value: '' });
r.cleared = kids.some(c => c.hidden);
g.tqSetLogLevel({ value: 'error' });
r.level = [e.body.classList.has('lvl-error'), e.body.classList.has('lvl-warn')];
g.tqToggleLogWrap();
r.nowrap = e.body.classList.has('nowrap');
r.bad = String(g.tqLogFilterRegex('a(b'));                          // invalid regex -> literal
r.empty = g.tqLogFilterRegex('  ');
"""))
    assert out["filtered"] == [True, False, False] and out["cleared"] is False
    assert out["level"] == [True, False] and out["nowrap"] is True
    assert out["bad"] == r"/a\(b/i" and out["empty"] is None
