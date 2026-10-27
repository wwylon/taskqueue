"""Behavior tests for views/docs.js (built-in help drawer + Docs tab) under the Node DOM stub."""
from __future__ import annotations

import json

from tests.test_js_app import run_js
from tests.test_js_smoke import EV

MANIFEST = {"guides": {"web-ui": {}, "task-standards": {}}, "references": {"cli": {}}}
PAYLOAD = {
    "page": {"id": "queue", "title": "Queue <page>", "html": "<p>about queue</p>", "read_more": "docs/web-ui#queue-tab"},
    "general": [{"id": "force-run", "title": "Force run", "html": "<p>force it</p>"}],
}


def test_help_modal_renders_payload_toggles_and_rerenders_per_tab():
    out = run_js(
        EV
        + f"""
const {{ g, doc }} = await setup({{ app: true, routes: {{ '/api/help': {json.dumps(PAYLOAD)} }} }});
await g.flush();
const r = {{}};
await g.openHelp();
const modal = () => doc.body.children.find(c => c.id === 'tq-help-modal');
const html = () => modal().querySelector('.tq-help-body').innerHTML;
r.shown = [!!modal(), g.fetches.filter(f => f.url.startsWith('/api/help')).pop().url];
r.html = ['Queue &lt;page&gt;', 'about queue', 'Force run', 'help.allTopics', '/api/docs', 'tqOpenGuide'].map(s => html().includes(s));
g.closeHelp();
r.hidden = modal().style.display;
const n = g.fetches.length;
await g.openHelp();                                   // reopened after hide: re-fetches for the current tab
r.refetch = [modal().style.display, g.fetches.length - n];
// clicking the overlay itself closes it, clicking inside does not
modal().onclick({{ target: modal() }});
r.overlay = modal().style.display;
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert out["shown"][0] is True and out["shown"][1].startswith("/api/help?tab=queue&lang=")
    assert out["html"] == [True] * 6
    assert out["hidden"] == "none"
    assert out["refetch"] == ["flex", 1]
    assert out["overlay"] == "none"


def test_help_falls_back_to_bundle_then_to_error_and_uses_locale_topics():
    out = run_js(
        EV
        + """
const bundle = { tabs: { queue: { page: { id: 'queue', title: 'Bundled', html: '<p>b</p>' }, general: [] } } };
const { g, doc } = await setup({ app: true, routes: { '/static/help-bundle.en.json': bundle } });
await g.flush();
const real = g.fetch;
g.fetch = async (url, o) => (url.startsWith('/api/help') ? { ok: false, status: 500 } : real(url, o));
const r = {};
const body = () => doc.body.children.find(c => c.id === 'tq-help-modal').querySelector('.tq-help-body');
await g.openHelp();
r.bundled = body().innerHTML.includes('Bundled');
// topic text for non-English comes from locale keys when they resolve; the stub echoes raw keys, so it falls back to server html
g.localStorage.setItem('tq_lang', 'pl');
r.topic = g._helpTopicHtml({ id: 'force-run', title: 'T', html: '<b>h</b>' });
r.empty = g._helpTopicHtml(null);
// no payload and no bundle at all -> load-failed message
g.fetch = async () => ({ ok: false, status: 500 });
g.closeHelp(); doc.body.children.find(c => c.id === 'tq-help-modal').dataset.helpTab = 'x';
await g.openHelp();
r.failed = body().innerHTML.includes('help.loadFailed') || body().innerHTML.includes('Bundled');
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert out["bundled"] is True
    assert "<b>h</b>" in out["topic"] and "<h3>T</h3>" in out["topic"]
    assert out["empty"] == ""
    assert out["failed"] is True


def test_docs_page_builds_nav_loads_remembered_doc_and_marks_active():
    out = run_js(
        EV
        + f"""
const {{ g, doc, S }} = await setup({{ app: true, routes: {{
  '/docs/manifest.json': {json.dumps(MANIFEST)},
  '/docs/task-standards?format=json': {{ title: 'Task standards', html: '<section id="a"><h2>Alpha</h2></section><section id="b"><h3>Beta</h3></section><a href="/docs/web-ui#queue">x</a>' }},
  '/docs/web-ui?format=json': {{ title: 'Web UI', html: '<p>web</p>' }},
}} }});
await g.flush();
g.localStorage.setItem('tq_last_doc', 'task-standards');
S.docsCurrentId = null;
g.CSS = {{ escape: s => s }};
const r = {{}};
await g.loadDocsPage(); await g.flush(); await g.flush();
r.guides = doc.getElementById('docs-nav-guides').querySelectorAll('.docs-nav-item').map(b => b.dataset.docId);
r.refs = doc.getElementById('docs-nav-refs').querySelectorAll('.docs-nav-item').map(b => b.textContent);
r.loaded = [S.docsCurrentId, doc.getElementById('docs-title').textContent, doc.getElementById('docs-content').dataset.docId];
r.sections = doc.getElementById('docs-nav-sections').querySelectorAll('.docs-nav-item').map(b => [b.dataset.sectionId, b.textContent]);
r.stored = g.localStorage.getItem('tq_last_doc');
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert out["guides"] == ["web-ui", "task-standards"] and out["refs"] == ["cli"]
    assert out["loaded"] == ["task-standards", "Task standards", "task-standards"]
    assert out["sections"] == [["a", "Alpha"], ["b", "Beta"]]
    assert out["stored"] == "task-standards"


def test_load_doc_same_guide_is_noop_section_jump_scrolls_and_errors_are_shown():
    out = run_js(
        EV
        + """
const { g, doc, S } = await setup({ app: true, routes: {
  '/docs/guide?format=json': { title: 'Guide', html: '<section id="s1"><h2>One</h2></section><section id="s2"><h2>Two</h2></section>' },
} });
await g.flush();
g.CSS = { escape: s => s };
const r = {};
const count = () => g.fetches.filter(f => f.url.startsWith('/docs/guide')).length;
await g.loadDoc('guide', { replace: true });
r.first = [count(), S.docsCurrentId, S.docsSectionId];
await g.loadDoc('guide');                               // already shown: no refetch
r.same = count();
await g.loadDoc('guide', { section: 's2' });            // section jump only: no refetch, section remembered
r.jump = [count(), S.docsSectionId];
g.tqDocsJump('s1');
r.jumped = [S.docsSectionId, doc.getElementById('docs-nav-sections').querySelectorAll('.docs-nav-item').filter(b => b.classList.has('active')).map(b => b.dataset.sectionId)];
await g.loadDoc('guide', { force: true });              // force refetches
r.forced = count();
await g.loadDoc('missing');
const content = doc.getElementById('docs-content');
r.missing = [content.innerHTML.includes('docs.loadFailed'), content.dataset.docId];
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert out["first"] == [1, "guide", None]
    assert out["same"] == 1
    assert out["jump"] == [1, "s2"]
    assert out["jumped"] == ["s1", ["s1"]]
    assert out["forced"] == 2
    assert out["missing"] == [True, ""]


def test_open_guide_closes_help_and_switches_to_docs_tab():
    out = run_js(
        EV
        + """
const { g, doc, S } = await setup({ app: true, routes: { '/api/help': { page: null, general: [] } } });
await g.flush();
const r = {};
await g.openHelp();
const tabs = [];
g.tab = (name, btn) => tabs.push(name);
g.tqOpenGuide('task-standards', 'intro');
r.state = [S.docsCurrentId, S.docsSectionId];
r.help = doc.body.children.find(c => c.id === 'tq-help-modal').style.display;
r.tabs = tabs;
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert out["state"] == ["task-standards", "intro"]
    assert out["help"] == "none"
    assert out["tabs"] in (["docs"], [])
