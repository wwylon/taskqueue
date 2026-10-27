"""Behavior tests for core/nav.js (page size, tab switch, jump to status, restart labels, log scroll states)."""
from __future__ import annotations

from tests.test_js_app import run_js
from tests.test_js_smoke import EV

PRE = """
const { g, doc, S } = await setup({ app: true, routes: {} });
await g.flush();
const r = {};
"""


def _js(body: str) -> str:
    return EV + PRE + body + "\nconsole.log('RESULT:' + JSON.stringify(r));\n"


def test_page_size_depends_on_width_and_restart_labels():
    out = run_js(_js("""
g.window.innerWidth = 500; r.mob = g._pageSize();
g.window.innerWidth = 1200; r.desk = g._pageSize();
r.restart = ['stopped', 'interrupted', 'suspended', 'cancelled', 'failed', 'queued'].map(s => g._isRestartStatus(s));
r.labels = [g._requeueLabel('stopped'), g._requeueLabel('failed')];
"""))
    assert out["mob"] == 10 and out["desk"] == 50
    assert out["restart"] == [True, True, True, True, False, False]
    assert out["labels"] == ["btn.restart", "btn.retry"]


def test_tab_switches_page_persists_last_tab_and_closes_shortcuts_off_queue():
    out = run_js(_js("""
const calls = [];
g.tqCloseShortcuts = () => calls.push('close'); g.tqSyncHeaderMark = () => calls.push('mark');
g.tqLoadTabBusy = n => calls.push('load:' + n); g._syncUrl = o => calls.push('url:' + o.replace);
const page = doc.getElementById('audit') || doc.add('audit');
const btn = doc.add('b-audit', { tag: 'button' });
S.applyingUrl = false;
g.tab('audit', btn);
r.page = [page.classList.has('active'), btn.classList.has('active'), g.localStorage.getItem('tq_last_tab')];
r.calls = calls.slice();
calls.length = 0; S.applyingUrl = true;
const q = doc.getElementById('queue') || doc.add('queue'); const qb = doc.add('b-queue', { tag: 'button' });
g.tab('queue', qb);
r.queueCalls = calls.slice();
"""))
    assert out["page"] == [True, True, "audit"]
    assert out["calls"] == ["close", "mark", "load:audit", "url:false"]
    assert out["queueCalls"] == ["mark", "load:queue"]


def test_jump_to_status_clears_filters_and_applies_chip():
    out = run_js(_js("""
const seen = [];
g._resetQueueSearchForFilter = () => seen.push('reset');
g._updateTagChips = () => seen.push('tags'); g._updateGroupChips = () => seen.push('groups');
g.setQueueFilter = (st, chip, o) => seen.push(['filter', st, chip && chip.id, o]);
g.tqSyncHeaderMark = () => {};
S.tagFilter = 'gpu'; S.groupFilter = 'g1';
const q = doc.getElementById('queue') || doc.add('queue'); q.classList.remove('active');
g.jumpToStatus('failed', { x: 1 });
r.seen = seen; r.state = [S.tagFilter, S.groupFilter, q.classList.has('active'), g.localStorage.getItem('tq_last_tab')];
"""))
    assert out["seen"][:3] == ["reset", "tags", "groups"]
    assert out["seen"][3][:2] == ["filter", "failed"]
    assert out["state"] == [None, None, True, "queue"]


def test_set_queue_active_flips_flag_and_selects_all_projects():
    out = run_js(_js("""
const calls = []; g.setProject = (p, o) => calls.push([p, o]);
g.setQueueActive();
r.v = [S.queueActiveOnly, calls];
"""))
    assert out["v"] == [True, [["all", {"keepActive": True}]]]


def test_log_scroll_states_capture_and_restore():
    out = run_js(_js("""
const a = doc.add('pa', { tag: 'pre' }), b = doc.add('pb', { tag: 'pre' });
a.scrollHeight = 500; a.clientHeight = 100; a.scrollTop = 400;     // at bottom
b.scrollHeight = 500; b.clientHeight = 100; b.scrollTop = 50;      // mid
const real = g.document.querySelectorAll;
g.document.querySelectorAll = sel => (sel === 'pre' ? [a, b] : real.call(g.document, sel));
const st = g._captureLogScrollStates();
r.cap = [st.pa.isAtBottom, st.pb.isAtBottom, st.pb.scrollTop];
a.scrollHeight = 900; a.scrollTop = 0; b.scrollTop = 0;
g._restoreLogScrollStates(Object.assign({}, st, { gone: { scrollTop: 1, isAtBottom: false } }));
r.rest = [a.scrollTop, b.scrollTop];
"""))
    assert out["cap"] == [True, False, 50]
    assert out["rest"] == [900, 50]
