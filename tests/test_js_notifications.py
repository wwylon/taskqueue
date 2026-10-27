"""Behavior tests for components/notifications.js (bell badge, panel, alert toasts, alert detail html)."""
from __future__ import annotations

from tests.test_js_app import run_js
from tests.test_js_smoke import EV

EVENTS = [
    {"ts": "2026-01-01T10:00:00Z", "project": "p", "task_id": "a", "status": "completed"},
    {"ts": "2026-01-01T11:00:00Z", "project": "p", "task_id": "b", "status": "failed", "last_error_code": "tq.x"},
    {
        "ts": "2026-01-01T12:00:00Z", "project": "p", "task_id": "c", "status": "alert:any_failure",
        "description": "boom <b>", "detail": {"title": "Failure", "project": "p", "task_id": "c", "exit_code": 2, "log_tail": ["l1", "l2"]},
    },
]


def test_poll_sets_badge_then_toasts_only_new_alert_events():
    out = run_js(
        EV
        + f"""
const batches = [{EVENTS[:2]!r}, {EVENTS!r}];
let call = 0;
const {{ g, doc }} = await setup({{ app: true, routes: {{ '/api/notifications': () => ({{ events: batches[Math.min(call++, 1)] }}) }} }});
await g.flush();
const host = () => doc.body.children.find(c => c.id === 'tq-toast-host');
const badge = doc.getElementById('notif-badge');
const r = {{}};
call = 0; g.fetches.length = 0;
g._fetchNotifications(false); await g.flush();
r.badge1 = [badge.textContent, badge.style.display];
r.toasts1 = host() ? host().children.length : 0;      // first poll only sets the watermark: no toasts
g._fetchNotifications(false); await g.flush();
r.badge2 = [badge.textContent, badge.style.display];
const t = host().children.map(c => [c.className, c.textContent]);
r.toasts2 = t;
r.url = g.fetches.filter(f => f.url.startsWith('/api/notifications')).pop().url;
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert out["badge1"] == ["2", "inline-block"] and out["toasts1"] == 0
    assert out["badge2"] == ["3", "inline-block"]
    assert len(out["toasts2"]) == 1
    assert out["toasts2"][0][0] == "tq-toast tq-toast-err" and out["toasts2"][0][1] == "Failure\nboom <b>"
    assert out["url"].startswith("/api/notifications?limit=50")


def test_panel_toggle_renders_rows_marks_read_and_outside_click_closes():
    out = run_js(
        EV
        + f"""
const {{ g, doc }} = await setup({{ app: true, routes: {{ '/api/notifications': {{ events: {EVENTS!r} }} }} }});
await g.flush();
const $ = id => doc.getElementById(id);
const r = {{}};
g.toggleNotifPanel(); await g.flush();
r.open = $('notif-panel').style.display;
const rows = $('notif-list').querySelectorAll('.notif-row');
r.rows = rows.length;
r.order = rows.map(x => {{
  const s = x.querySelector('strong');
  const tag = s.querySelector('.proj-tag');
  // stub keeps only direct text on textContent; project lives in .proj-tag
  return tag ? tag.textContent + s.textContent : s.textContent;
}});        // newest first
r.clickable = rows.map(x => x.classList.has('clickable'));
r.unseen = rows.filter(x => x.classList.has('unseen')).length;
r.err = !!$('notif-list').querySelector('.notif-err');
r.escaped = $('notif-list').innerHTML.includes('boom &lt;b&gt;');
r.projTags = $('notif-list').querySelectorAll('.proj-tag').length;
// alert rows toggle their hidden detail block through the delegated action
const alertRow = rows[0];
const detail = alertRow.querySelector('.alert-detail');
r.hiddenBefore = !!detail.hidden;
fire(doc, 'click', alertRow);
r.hiddenAfter = !!detail.hidden;
// marking read stores the newest ts and hides the badge
g._notifMarkRead();
r.stored = g.localStorage.getItem('tq_notif_since');
r.badge = $('notif-badge').style.display;
// outside click closes the panel; a click on the bell does not
fire(doc, 'click', doc.body);
r.closed = $('notif-panel').style.display;
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert out["open"] == "block" and out["rows"] == 3
    assert out["order"][0] == "Failure" and out["order"][1:] == ["p/b", "p/a"]
    assert out["projTags"] >= 2  # task rows + alert detail task field
    assert out["clickable"] == [True, False, False] and out["unseen"] == 3 and out["err"] is True
    assert out["escaped"] is True
    assert out["hiddenBefore"] != out["hiddenAfter"]  # stub does not map the hidden attribute to the property
    assert out["stored"] == "2026-01-01T12:00:00Z" and out["badge"] == "none"
    assert out["closed"] == "none"


def test_alert_detail_html_lists_fields_log_tail_and_detail_button():
    out = run_js(
        EV
        + f"""
const {{ g }} = await setup({{ app: true }});
await g.flush();
const ev = {EVENTS[2]!r};
const html = g.tqAlertDetailHtml(ev);
const empty = g.tqAlertDetailHtml({{}});
console.log('RESULT:' + JSON.stringify({{ html, empty }}));
"""
    )
    h = out["html"]
    assert 'class="proj-tag"' in h and ">p</span>" in h and ">c</span>" in h
    assert "exit code" in h and "l1\nl2" in h and "tqOpenDrawer" in h
    assert "<pre" in h
    assert "tqOpenDrawer" not in out["empty"] and "<pre" not in out["empty"]


def test_alert_watermark_is_persisted_for_the_next_page_load():
    out = run_js(
        EV
        + f"""
const {{ g }} = await setup({{ app: true, routes: {{ '/api/notifications': () => ({{ events: {EVENTS!r} }}) }} }});
await g.flush();
g._fetchNotifications(false); await g.flush();
console.log('RESULT:' + JSON.stringify({{ saved: g.localStorage.getItem('tq_notif_toast_since') }}));
"""
    )
    assert out["saved"] == "2026-01-01T12:00:00Z"
