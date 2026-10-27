"""Behavior tests for components/shortcuts.js (shortcut overlay, queue-row navigation, global key handler)."""
from __future__ import annotations

from tests.test_js_app import run_js
from tests.test_js_smoke import EV

PRE = """
const { g, doc, S } = await setup({ app: true, routes: {} });
await g.flush();
const toasts = []; g.showToast = (m, k) => toasts.push([m, k]);
const r = {};
const key = (k, target) => { const e = { key: k, target: target || null, prevented: false, preventDefault() { this.prevented = true; } }; g.tqHandleKey(e); return e; };
"""


def _js(body: str) -> str:
    return EV + PRE + body + "\nconsole.log('RESULT:' + JSON.stringify(r));\n"


def test_shortcuts_overlay_toggle_header_mark_and_typing_target():
    out = run_js(_js("""
const sc = doc.add('tq-shortcuts'); sc.hidden = true;
const q = doc.getElementById('queue'); q.classList.remove('active');
r.inactive = g.tqQueueActive();
q.classList.add('active');
r.active = g.tqQueueActive();
g.tqToggleSelectVisible; g.tqToggleShortcuts(); r.shown = sc.hidden;
g.tqCloseShortcuts(); r.closed = sc.hidden;
g.tqHeaderMark(); r.markOnQueue = sc.hidden;                   // on the queue tab the header mark toggles shortcuts
q.classList.remove('active');
let helped = 0; g.openHelp = () => { helped++; };
g.tqHeaderMark(); r.markElsewhere = [helped, sc.hidden];
const btn = doc.add('header-help-btn', { tag: 'button' });
g.tqSyncHeaderMark();
r.helpKey = [btn.attrs ? 1 : 1, btn.title];
q.classList.add('active'); g.tqSyncHeaderMark(); r.queueKey = btn.title;
r.typing = [{ tagName: 'INPUT' }, { tagName: 'TEXTAREA' }, { tagName: 'SELECT' }, { tagName: 'DIV', isContentEditable: true }, { tagName: 'DIV' }, null].map(e => !!g.tqTypingTarget(e));
"""))
    assert out["inactive"] is False and out["active"] is True
    assert out["shown"] is False and out["closed"] is True and out["markOnQueue"] is False
    assert out["markElsewhere"] == [1, False]
    assert out["helpKey"][1] == "help.openTitle" and out["queueKey"] == "shortcuts.openTitle"
    assert out["typing"] == [True, True, True, True, False, False]


def test_move_queue_row_clamps_and_highlights_and_highlighted_task_resolves():
    out = run_js(_js("""
const tb = doc.getElementById('queue-tbody');
const rows = ['a', 'b', 'c'].map(id => { const tr = doc.createElement('tr'); tr.setAttribute('data-project', 'p'); tr.setAttribute('data-task', id); tr.scrollIntoView = () => {}; tb.appendChild(tr); return tr; });
g.tqMoveQueueRow(1);
r.first = [S.kbdTaskKey, rows.map(t => t.classList.has('qrow-kbd'))];
g.tqMoveQueueRow(1); g.tqMoveQueueRow(1); g.tqMoveQueueRow(1);
r.clamp = [S.kbdTaskKey, rows.map(t => t.classList.has('qrow-kbd'))];
g.tqMoveQueueRow(-1);
r.up = S.kbdTaskKey;
S.kbdTaskKey = null; g.tqMoveQueueRow(-1);                     // no highlight + moving up starts from the bottom
r.fromEnd = S.kbdTaskKey;
S.queue = [{ project: 'p', task_id: 'b', status: 'failed' }];
S.kbdTaskKey = 'p\\0b';
r.task = g.tqHighlightedTask().status;
S.kbdTaskKey = null; r.none = g.tqHighlightedTask();
S.kbdTaskKey = 'nosep'; r.bad = g.tqHighlightedTask();
rows.forEach(t => t.parentNode && tb.children.splice(0, tb.children.length));
S.kbdTaskKey = 'keep'; g.tqMoveQueueRow(1); r.noRows = S.kbdTaskKey;
"""))
    assert out["first"] == ["p\0a", [True, False, False]]
    assert out["clamp"] == ["p\0c", [False, False, True]]
    assert out["up"] == "p\0b" and out["fromEnd"] == "p\0c"
    assert out["task"] == "failed" and out["none"] is None and out["bad"] is None and out["noRows"] == "keep"


def test_escape_closes_the_topmost_layer_first():
    out = run_js(_js("""
const help = doc.add('tq-help-modal'); help.style.display = 'flex';
const gen = doc.getElementById('gen-modal') || doc.add('gen-modal');
const sc = doc.add('tq-shortcuts'); sc.hidden = false;
const drawer = doc.add('tq-drawer-back'); drawer.hidden = false;
const log = doc.add('tq-log-panel'); log.hidden = false;
const closed = [];
g.closeHelp = () => { closed.push('help'); help.style.display = 'none'; };
g.tqCloseDrawer = () => { closed.push('drawer'); drawer.hidden = true; };
g.tqCloseLog = () => { closed.push('log'); log.hidden = true; };
g.tqClearSelection = () => closed.push('selection');
S.queueSelection.add('p\\0a');
gen.classList.add('open');
key('Escape'); r.helpFirst = closed.slice();
key('Escape'); r.genModal = closed.slice();                   // generic modal open: Escape defers to it
gen.classList.remove('open');
key('Escape'); r.shortcuts = [sc.hidden, closed.length];
key('Escape'); key('Escape');
r.rest = closed.slice();
key('Escape'); r.last = closed.slice();
"""))
    assert out["helpFirst"] == ["help"] and out["genModal"] == ["help"]
    assert out["shortcuts"] == [True, 1]
    assert out["rest"] == ["help", "drawer", "log"] and out["last"] == ["help", "drawer", "log", "selection"]


def test_question_slash_and_queue_letter_keys():
    out = run_js(_js("""
const input = { tagName: 'INPUT' };
const sc = doc.add('tq-shortcuts'); sc.hidden = true;
const q = doc.getElementById('queue'); q.classList.add('active');
r.typingIgnored = key('?', input).prevented;
key('?'); r.questionToggled = sc.hidden;
q.classList.remove('active');
let helped = 0; g.openHelp = () => { helped++; };
key('?'); r.questionHelp = helped;
// "/" focuses the queue search (and switches tab), or the history search when history is active
const tabs = []; g.tab = (n) => tabs.push(n);
const nav = doc.add('nav-queue-btn', { tag: 'button' });
const search = doc.add('queue-search'); let focused = 0; search.focus = () => focused++; search.select = () => {};
const e = key('/'); r.slash = [e.prevented, focused];
const hist = doc.getElementById('history'); hist.classList.add('active');
const hs = doc.add('history-search'); let hf = 0; hs.focus = () => hf++; hs.select = () => {};
key('/'); r.history = [hf, focused];
hist.classList.remove('active'); q.classList.add('active');
// j/k/a/Enter/r/s only act on the queue tab
const calls = [];
g.tqToggleSelectVisible = on => calls.push('all' + on);
key('j'); key('k'); key('a');
q.classList.remove('active'); key('j'); q.classList.add('active');
r.letters = calls.slice();
S.queue = [{ project: 'p', task_id: 'a', status: 'failed' }, { project: 'p', task_id: 'b', status: 'running' }, { project: 'p', task_id: 'c', status: 'completed' }];
let opened = null; g.tqOpenDrawer = (p, t) => { opened = [p, t]; };
const posts = []; g._postJson = async (u) => { posts.push(u); return {}; };
let refreshed = 0; g.refresh = () => { refreshed++; };
S.kbdTaskKey = 'p\\0a';
const en = key('Enter'); r.enter = [opened, en.prevented];
key('r'); await g.flush(); r.retry = [posts.slice(), toasts.pop(), refreshed];
key('s'); r.stopOnFailed = posts.length;                        // failed task cannot be stopped
S.kbdTaskKey = 'p\\0b';
key('r'); r.retryRunning = posts.length;                         // running task cannot be retried
key('s'); await g.flush(); r.stop = [posts.slice(), toasts.pop()];
g._postJson = async () => { throw new Error('nope'); };
S.kbdTaskKey = 'p\\0c'; key('r'); await g.flush(); r.err = toasts.pop();
S.kbdTaskKey = null; r.noHighlight = key('r').prevented;
"""))
    assert out["typingIgnored"] is False and out["questionToggled"] is False and out["questionHelp"] == 1
    assert out["slash"][0] is True and out["history"] == [1, out["slash"][1]]
    assert out["letters"] == ["alltrue"]
    assert out["enter"] == [["p", "a"], True]
    assert out["retry"] == [["/api/retry/p/a"], ["toast.retried", "ok"], 1]
    assert out["stopOnFailed"] == 1 and out["retryRunning"] == 1
    assert out["stop"] == [["/api/retry/p/a", "/api/stop/p/b"], ["toast.stopped", "ok"]]
    assert out["err"] == ["nope", "err"] and out["noHighlight"] is False
