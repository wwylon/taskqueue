"""Behavior tests for components/runlogs.js (log fetch/ANSI, copy helpers, history toggles, log-issue jumps)."""
from __future__ import annotations

from tests.test_js_app import run_js
from tests.test_js_smoke import EV

PRE = """
const { g, doc, S } = await setup({ app: true, routes: {} });
await g.flush();
const alerts = []; g.openAlert = (m, t) => alerts.push([m, t]);
const r = {};
const $ = (id, o) => doc.getElementById(id) || doc.add(id, o);
"""


def _js(body: str) -> str:
    return EV + PRE + body + "\nconsole.log('RESULT:' + JSON.stringify(r));\n"


def test_panel_key_prefixes_and_ansi_rendering_escapes_html():
    out = run_js(_js("""
r.keys = ['d-log-pre-1', 'qc-log-pre-1', 'q-log-pre-1', 'hc-x', 'h-x', 'zzz'].map(p => g._panelKey(p, 9));
r.esc = g._ansiToHtml('<b>&</b>');
r.color = g._ansiToHtml('\\x1b[31mred\\x1b[0m plain');
r.kw = [g._ansiToHtml('ERROR boom'), g._ansiToHtml('WARN x'), g._ansiToHtml('DEBUG y'), g._ansiToHtml('fine')];
r.unclosed = g._ansiToHtml('\\x1b[31mred');
"""))
    assert out["keys"] == ["d:9", "qc:9", "q:9", "h:9", "h:9", "d:9"]
    assert out["esc"] == "&lt;b&gt;&amp;&lt;/b&gt;"
    assert "red</span>" in out["color"] and out["color"].endswith(" plain")
    assert "#e74c3c" in out["kw"][0] and "#f39c12" in out["kw"][1] and "0.6" in out["kw"][2] and out["kw"][3] == "fine"
    assert out["unclosed"].endswith("</span>")


def test_sse_append_counts_and_skips_trim_for_small_logs():
    out = run_js(_js("""
const pre = { _t: '', _n: 0, insertAdjacentHTML(p, h) { this._t += h; }, get textContent() { return this._t; } };
for (let i = 0; i < 40; i++) g._sseAppend(pre, 'a\\n');
r.len = pre._t.length; r.count = pre._sseCount;
"""))
    assert out["len"] == 80 and out["count"] == 40


def test_fetch_log_renders_banner_stores_content_and_reports_errors():
    out = run_js(_js("""
const pre = $('d-log-pre-3');
S.expandedLogs['d:3'] = { content: '' };
const hdr = { 'X-Log-Size': String(3 * 1024 * 1024), 'X-Log-Truncated': '1' };
g.fetch = async () => ({ ok: true, headers: { get: k => hdr[k] || null }, text: async () => 'hello <x>\\nERROR bad' });
await g._fetchLog(3);
r.html = [pre.innerHTML.includes('hello &lt;x&gt;'), pre.innerHTML.includes('log is'), S.expandedLogs['d:3'].content];
hdr['X-Log-Truncated'] = '0';
await g._fetchLog(3, 'd-log-pre-3');
r.noBanner = !pre.innerHTML.includes('log is');
g.fetch = async () => { throw new Error('offline'); };
await g._fetchLog(3, 'd-log-pre-3');
r.err = pre.textContent;
"""))
    assert out["html"] == [True, True, "hello <x>\nERROR bad"]
    assert out["noBanner"] is True and out["err"].startswith("error loading log:")


def test_copy_log_tail_prefers_stored_content_and_alerts_when_empty():
    out = run_js(_js("""
const copied = []; g.navigator.clipboard.writeText = async t => copied.push(t);
const lines = Array.from({ length: 150 }, (_, i) => 'l' + i).join('\\n');
S.expandedLogs['d:5'] = { content: lines };
await g.copyLogTail(5, 'd-log-pre-5', null);
const tail = copied[0].split('\\n');
r.tail = [tail.length <= 100, tail[tail.length - 1]];
S.expandedLogs['d:7'] = { content: '' }; $('d-log-pre-7').textContent = '   ';
await g.copyLogTail(7, 'd-log-pre-7', null);
r.empty = [copied.length, alerts.length];
await g.copyTaskName('p', 't', null);
r.name = copied[1];
g.navigator.clipboard.writeText = async () => { throw new Error('denied'); };
await g.copyTaskName('p', 't', null);
r.fail = alerts.slice(-1)[0][0];
"""))
    assert out["tail"] == [True, "l149"]
    assert out["empty"] == [1, 1] and out["name"] == "p/t"
    assert out["fail"].startswith("Copy failed:")


def test_history_helpers_render_rows_limit_and_fallbacks():
    out = run_js(_js("""
r.empty = g._renderTaskRunHistoryHtml('p', 't', [], null);
r.many = g._renderTaskRunHistoryHtml('p', 't', Array.from({ length: 25 }, (_, i) => ({ run_id: i, status: 'x' })), null).includes('20 of 25');
const one = g._renderTaskRunHistoryHtml('p', 't', [{ run_id: 4, status: 'failed', worker_id: 'w<1>', exit_code: 1 }], null);
r.one = [one.includes('w&lt;1&gt;'), one.includes('#4')];
g.fetch = async () => { throw new Error('down'); };
r.failed = (await g._loadTaskRunHistoryHtml('p', 't')).includes('Failed to load history');
"""))
    assert "No runs recorded yet." in out["empty"]
    assert out["many"] is True and out["one"] == [True, True] and out["failed"] is True


def test_resolve_log_panel_and_open_log_issue_prefills_search():
    out = run_js(_js("""
r.none = g._resolveLogPanel(1);
$('d-log-wrap-2'); $('q-log-row-3');
r.panels = [g._resolveLogPanel(2).panel, g._resolveLogPanel(3, { rowId: 'x' }).panel];
const applied = []; g._applyLogSearch = (...a) => applied.push(a);
const inp = $('log-search-inp-d-log-pre-2');
S.expandedLogs['d:2'] = { content: 'ERROR here' };
await g.openLogIssue(2, 'd', 'err', 'p', 't', '', true);
r.issue = [S.logSearch['d-log-pre-2'], inp.value];
await g.openLogIssue(0, 'd', 'warn'); r.noRun = S.logSearch['d-log-pre-0'] === undefined;
"""))
    assert out["none"] is None and out["panels"] == ["d", "q"]
    assert out["issue"] == [{"query": "error", "current": 0}, "ERROR"]
    assert out["noRun"] is True


def test_fetch_log_drops_a_response_that_arrives_after_a_newer_request():
    out = run_js(_js("""
const pre = $('d-log-pre-4');
const pending = [];
g.fetch = url => new Promise(res => pending.push(t => res({ ok: true, headers: { get: () => null }, text: async () => t })));
const older = g._fetchLog(4); const newer = g._fetchLog(4);
pending[1]('NEW'); await newer;
pending[0]('OLD'); await older;
r.html = pre.innerHTML;
"""))
    assert "NEW" in out["html"] and "OLD" not in out["html"]


def test_fetch_log_incremental_appends_from_offset():
    out = run_js(_js("""
const pre = $('d-log-pre-4');
const urls = [];
let hdr = { 'X-Log-Next': '6' }, body = 'a\\nb\\nc\\n';
g.fetch = async u => (urls.push(u), { ok: true, headers: { get: k => hdr[k] || null }, text: async () => body });
await g._fetchLog(4);
hdr = { 'X-Log-From': '6', 'X-Log-Next': '8' }; body = 'd\\n';
await g._fetchLog(4);
r.after = pre.innerHTML.includes('a') && pre.innerHTML.includes('d');
const before = pre.innerHTML;
hdr = { 'X-Log-From': '8', 'X-Log-Next': '8' }; body = '';
await g._fetchLog(4);
r.same = pre.innerHTML === before;
g._sseAppend(pre, 'x\\n');
await g._fetchLog(4);
r.urls = urls;
"""))
    assert out["urls"] == ["/api/logs/4", "/api/logs/4?from=6", "/api/logs/4?from=8", "/api/logs/4"]
    assert out["after"] and out["same"]
