"""Behavior tests that run the real classic app.js (plus the ES modules) in Node against a DOM stub.

tests/js_dom_stub.mjs builds the element tree from index.html, records timers/fetches/EventSources and evaluates
i18n.js, js/main.js and app.js in one global context. Each scenario runs in its own node process (app.js declares
top-level consts, so it can only be evaluated once per process) and prints one `RESULT:<json>` line.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STUB_URL = (ROOT / "tests" / "js_dom_stub.mjs").as_uri()
LOGPANEL_URL = (ROOT / "taskqueue" / "static" / "js" / "components" / "logpanel.js").as_uri()

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

PRELUDE = f"import {{ setup }} from {STUB_URL!r};\nconst LOGPANEL = {LOGPANEL_URL!r};\n"


def run_js(body: str, timeout: int = 60):
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", PRELUDE + body],
        capture_output=True, text=True, timeout=timeout, cwd=str(ROOT), encoding="utf-8",
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith("RESULT:"):
            return json.loads(line[len("RESULT:"):])
    raise AssertionError("no RESULT line; stdout=%r stderr=%r" % (proc.stdout[-1500:], proc.stderr[-1500:]))


def test_fmt_cpu_uses_core_count_when_known():
    out = run_js(
        """
const { g, S } = await setup({ app: true });
S.cpuTotalCores = 8; const a = g.fmtCpu(25);
S.cpuTotalCores = 0; const b = g.fmtCpu(37.4);
console.log('RESULT:' + JSON.stringify({ a, b }));
"""
    )
    assert out == {"a": "2.0 cores", "b": "37%"}


def test_ui_deep_link_boot_popstate_and_sync():
    out = run_js(
        """
const { g, doc, S } = await setup({ app: true, url: 'http://localhost:8765/?page=history&project=p1&q=hello&group=g1&bogus=1' });
await g.flush();
const active = () => (doc.querySelector('.page.active') || {}).id;
const r = {};
r.boot = { page: active(), project: S.selectedProject, group: S.groupFilter, q: doc.getElementById('queue-search').value };
r.read = {
  alias: g._readUiUrl('?tab=audit'), badTab: g._readUiUrl('?page=nope'), emptyQ: g._readUiUrl('?q='),
  docs: g._readUiUrl('?page=docs&doc=d&section=s'),
};
const tabs = g.UI_TABS;
r.tabs = tabs.length;
r.missingNav = tabs.filter(t => !doc.querySelector('nav button[data-tab="' + t + '"]'));
r.missingPage = tabs.filter(t => !doc.getElementById(t));
r.hasDocsJump = typeof g.tqDocsJump;
// back/forward: the URL drives tab, project, filters and search without writing history again
const before = g.history.pushed.length + g.history.replaced.length;
g.location.search = '?page=alerts&project=zz';
g.dispatchWinEvent({ type: 'popstate' });
await g.flush();
r.pop = { page: active(), project: S.selectedProject, group: S.groupFilter, q: doc.getElementById('queue-search').value,
          applying: S.applyingUrl, wrote: g.history.pushed.length + g.history.replaced.length - before };
// docs doc/section are part of the shareable URL only on the docs tab
g._activateTabFromUrl('docs'); S.docsCurrentId = 'd1'; S.docsSectionId = 's1';
const n0 = g.history.pushed.length;
g._syncUrl();
const pushed = g.history.pushed.slice(n0);
g._syncUrl();
r.sync = { pushed, again: g.history.pushed.length - n0 };
g._activateTabFromUrl('queue'); const rep0 = g.history.replaced.length;
g._syncUrl({ replace: true });
r.replaced = g.history.replaced.length - rep0;
S.applyingUrl = true; S.docsCurrentId = null; const n1 = g.history.pushed.length + g.history.replaced.length;
g._syncUrl();
r.applyingSkips = g.history.pushed.length + g.history.replaced.length === n1;
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert out["boot"] == {"page": "history", "project": "p1", "group": "g1", "q": "hello"}
    assert out["read"]["alias"] == {"tab": "audit"}
    assert out["read"]["badTab"] == {}
    assert out["read"]["emptyQ"] == {"q": ""}
    assert out["read"]["docs"] == {"tab": "docs", "doc": "d", "section": "s"}
    assert out["tabs"] == 15 and out["missingNav"] == [] and out["missingPage"] == []
    assert out["hasDocsJump"] == "function"
    assert out["pop"] == {"page": "alerts", "project": "zz", "group": None, "q": "", "applying": False, "wrote": 0}
    assert len(out["sync"]["pushed"]) == 1
    p = out["sync"]["pushed"][0]
    assert "page=docs" in p and "doc=d1" in p and "section=s1" in p
    assert out["sync"]["again"] == 1, "an unchanged URL must not push another history entry"
    assert out["replaced"] == 1 and out["applyingSkips"] is True


def test_single_sse_connection_and_stale_error_ignored():
    out = run_js(
        """
const { g } = await setup({ app: true });
await g.flush();
const ev = () => g.eventSources.filter(e => e.url.startsWith('/api/events'));
const retry = () => g.timers.filter(t => t.live && t.fn.name === 'startSSE');
const r = { first: ev().length, retryBefore: retry().length };
ev()[0].fail();                       // current stream errors: closed, one retry scheduled
r.retryAfterError = retry().length; r.retryMs = retry()[0] && retry()[0].ms;
retry()[0].fn(); await g.flush();     // retry opens the replacement
r.second = ev().length; r.firstClosed = ev()[0].closed;
ev()[0].fail();                       // late error from the old stream
r.retryAfterStale = retry().length; r.liveStreams = ev().filter(e => !e.closed).length;
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert out["first"] == 1 and out["retryBefore"] == 0
    assert out["retryAfterError"] == 1 and out["retryMs"] == 5000
    assert out["second"] == 2 and out["firstClosed"] is True
    assert out["retryAfterStale"] == 0, "an error from a superseded stream must not schedule another connection"
    assert out["liveStreams"] == 1


def test_mutating_actions_report_failed_responses():
    out = run_js(
        """
let mode = { __status: 500, body: { error: 'boom' } };
const hit = () => mode;
const { g, doc, S } = await setup({ app: true, routes: {
  '/api/stop/': hit, '/api/retry/': hit, '/api/mark-processed/': hit, '/api/project/': hit } });
await g.flush();
const toasts = []; g.showToast = (m, k) => toasts.push([String(m), k]);
const btn = () => doc.createElement('button');
const r = {};
S.queue = [{ project: 'p', task_id: 't', status: 'failed' }];
S.selectedProject = 'p'; S.cfg = {};
const kinds = async f => { toasts.length = 0; await f(); await g.flush(); return toasts.map(t => t[1]); };
g.tqStopUndoMs = 0;
r.stopErr = await kinds(() => g.stopTask(btn(), 'p', 't'));
r.retryErr = await kinds(() => g.retryTask('p', 't', btn()));
r.retryStatus = S.queue[0].status;
r.processedErr = await kinds(() => g.markProcessed(btn(), 'p', 't'));
r.freezeErr = await kinds(() => g.toggleProjectFreeze());
mode = { __status: 200, body: { ok: false, error: 'nope' } };     // 200 with ok:false is also a failure
r.stopSoftErr = await kinds(() => g.stopTask(btn(), 'p', 't'));
mode = { ok: true };
r.stopOk = await kinds(() => g.stopTask(btn(), 'p', 't'));
r.processedOk = await kinds(() => g.markProcessed(btn(), 'p', 't'));
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    for k in ("stopErr", "retryErr", "processedErr", "freezeErr", "stopSoftErr"):
        assert out[k] == ["err"], (k, out[k])
    assert out["retryStatus"] == "failed", "a failed retry must not flip the row to queued"
    assert out["stopOk"] == ["ok"] and out["processedOk"] == []


def test_streamed_log_content_is_capped():
    out = run_js(
        """
const { g, doc } = await setup({});
const lp = await import(LOGPANEL);
doc.add('pre1', { tag: 'pre' });
g._sseAppend = () => {}; g._ansiToHtml = x => x;
const expanded = { k: { content: '' } };
lp.openLogStream({ runId: 'r9', maps: [{}], map: {}, preId: 'pre1', followId: 'nofollow', expanded, stateKey: 'k' });
await g.flush();
const es = g.eventSources.find(e => e.url.includes('/api/logs/r9/stream'));
for (let i = 0; i < 600; i++) es.emit('x'.repeat(1000));
console.log('RESULT:' + JSON.stringify({ len: expanded.k.content.length, tail: expanded.k.content.endsWith('x\\n') }));
"""
    )
    assert 0 < out["len"] <= 256 * 1024 and out["tail"] is True


def test_data_action_targets_exist_as_globals():
    """Every function named by a data-<event> attribute in index.html resolves to a window function at runtime."""
    out = run_js(
        """
const { g, doc } = await setup({ app: true });
await g.flush();
const EV = ['click','change','input','keydown','keyup','submit','contextmenu','mouseenter','mouseleave'];
const names = new Set();
for (const el of doc.all()) for (const e of EV) {
  const raw = el.getAttribute('data-' + e); if (raw == null) continue;
  const txt = raw.replace(/&#39;/g, "'").replace(/&quot;/g, '"').replace(/&amp;/g, '&');
  for (const call of JSON.parse(txt)) names.add(call[0]);
}
console.log('RESULT:' + JSON.stringify({ n: names.size, missing: [...names].filter(n => typeof g[n] !== 'function') }));
"""
    )
    assert out["n"] > 0
    assert out["missing"] == []
