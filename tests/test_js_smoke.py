"""UI smoke suite: drives the real index.html markup + app.js + modules through delegated events in Node.

jsdom is not installable without npm (the repo has no node_modules and no build step), so this uses the small
DOM stub in tests/js_dom_stub.mjs. Events are dispatched on `document` with the element a user would hit as
`target`, so everything goes through js/core/actions.js exactly as in the browser.
"""
from __future__ import annotations

from tests.test_js_app import run_js

EV = """
const fire = (doc, type, target, extra = {}) => {
  const ev = { type, target, key: extra.key, preventDefault() {}, stopPropagation() {}, stopImmediatePropagation() { this.stopped = true; } };
  doc.dispatchEvent(ev); return ev;
};
const active = doc => (doc.querySelector('.page.active') || {}).id;
const urlOf = f => f.url;
"""


def test_tab_switching_updates_page_nav_storage_url_and_fetch():
    out = run_js(
        EV
        + """
const { g, doc } = await setup({ app: true });
await g.flush();
const r = { start: active(doc) };
const btn = doc.querySelector('nav button[data-tab="history"]');
g.fetches.length = 0;
fire(doc, 'click', btn);
await g.flush();
r.page = active(doc);
r.navActive = doc.querySelectorAll('nav button').filter(b => b.classList.has('active')).map(b => b.getAttribute('data-tab'));
r.pagesActive = doc.querySelectorAll('.page').filter(p => p.classList.has('active')).length;
r.stored = g.localStorage.getItem('tq_last_tab');
r.url = g.history.pushed[g.history.pushed.length - 1];
r.fetchedHistory = g.fetches.some(f => f.url.startsWith('/api/dashboard') && f.url.includes('tab=history'));
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert out["start"] == "queue"
    assert out["page"] == "history" and out["navActive"] == ["history"] and out["pagesActive"] == 1
    assert out["stored"] == "history"
    assert "page=history" in out["url"]
    assert out["fetchedHistory"] is True


def test_history_search_debounces_and_load_more_pages():
    out = run_js(
        EV
        + """
const { g, doc, S } = await setup({ app: true });
await g.flush();
fire(doc, 'click', doc.querySelector('nav button[data-tab="history"]'));
await g.flush();
const r = {};
const inp = doc.getElementById('history-search');
inp.value = 'needle';
S.historyLimit = S.historyPageSize * 3;
g.fetches.length = 0;
fire(doc, 'input', inp);
r.searching = inp.classList.has('tq-searching');
r.limitReset = S.historyLimit === S.historyPageSize;
r.clearShown = doc.getElementById('history-search-clear').hidden === false;
r.fetchedEarly = g.fetches.length;
const debounce = g.timers.filter(t => t.live && t.ms === 350);
r.debounceTimers = debounce.length;
debounce[0].fn(); await g.flush();
const hist = g.fetches.map(urlOf).filter(u => u.includes('tab=history'));
r.searchUrl = hist[hist.length - 1];
r.searchingAfter = inp.classList.has('tq-searching');
// paging: "load more" grows the window by one page and refetches
S.history = [{}, {}, {}]; S.historyTotal = 500;
(await import(LOGPANEL.replace('components/logpanel.js', 'views/history.js')))._renderHistoryMore();
const more = doc.getElementById('history-pagination').querySelector('button');
r.hasMore = !!more;
const before = S.historyLimit;
g.fetches.length = 0;
fire(doc, 'click', more); await g.flush();
r.limitGrew = S.historyLimit - before === S.historyPageSize;
const h2 = g.fetches.map(urlOf).filter(u => u.includes('tab=history'));
r.pageUrl = h2[h2.length - 1];
r.pageSize = S.historyPageSize;
// clear button empties the box and resets the window
fire(doc, 'click', doc.getElementById('history-search-clear')); await g.flush();
r.cleared = inp.value === '';
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert out["searching"] and out["limitReset"] and out["clearShown"]
    assert out["fetchedEarly"] == 0 and out["debounceTimers"] == 1
    assert "search=needle" in out["searchUrl"]
    assert out["searchingAfter"] is False
    assert out["hasMore"] and out["limitGrew"]
    assert f"limit={out['pageSize'] * 2}" in out["pageUrl"]
    assert out["cleared"] is True


def test_project_switch_resets_group_filter_unless_kept():
    out = run_js(
        EV
        + """
const { g, doc, S } = await setup({ app: true });
await g.flush();
const sel = doc.getElementById('project-select');
const r = {};
S.selectedProject = 'a'; S.groupFilter = 'g1';
g.setProject('a');                       // same project: the group stays
r.same = S.groupFilter;
sel.value = 'b';
fire(doc, 'change', sel);                // user picks another project in the header select
r.afterSelect = { project: S.selectedProject, group: S.groupFilter };
S.groupFilter = 'g2';
g.setProject('c', { keepGroup: true });
r.kept = S.groupFilter;
S.groupFilter = 'g3';
g.setProject('d');
r.dropped = S.groupFilter;
r.pages = { q: S.queuePage, h: S.historyPage };
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert out["same"] == "g1"
    assert out["afterSelect"] == {"project": "b", "group": None}
    assert out["kept"] == "g2"
    assert out["dropped"] is None
    assert out["pages"] == {"q": 0, "h": 0}


def test_modal_backdrop_closes_only_when_backdrop_itself_is_clicked():
    out = run_js(
        EV
        + """
const { g, doc } = await setup({ app: true });
await g.flush();
const modal = doc.getElementById('cfg-modal');
const inner = modal.querySelector('.modal');
const r = {};
modal.classList.add('open');
fire(doc, 'click', inner);               // click inside the dialog: data-click-self must ignore it
r.afterInner = modal.classList.has('open');
fire(doc, 'click', doc.querySelector('#cfg-modal h2'));
r.afterHeading = modal.classList.has('open');
fire(doc, 'click', modal);               // click on the backdrop
r.afterBackdrop = modal.classList.has('open');
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert out == {"afterInner": True, "afterHeading": True, "afterBackdrop": False}
