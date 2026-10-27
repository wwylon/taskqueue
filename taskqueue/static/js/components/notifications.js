// In-UI notification bell: unseen badge, panel, alert toasts and alert detail html. Uses app.js globals at call
// time: tqAct, TQ_THIS, i18n, friendlyErrorText, tab.
import { tqInQuietHours } from './prefs.js';
import { escHtml, dur, fmt } from '../util/format.js';
import { showToast } from './modals.js';
import { STATUS_HEX } from '../util/colors.js';
import { projStyle } from '../util/projcolor.js';

// W6.5: in-UI notification bell
let _notifSince = '';
try { _notifSince = localStorage.getItem('tq_notif_since') || ''; } catch(e) {}
let _notifPanelOpen = false;

export function toggleNotifPanel() {
  _notifPanelOpen = !_notifPanelOpen;
  const p = document.getElementById('notif-panel');
  if (p) p.style.display = _notifPanelOpen ? 'block' : 'none';
  if (_notifPanelOpen) _fetchNotifications(true);
}

export function _notifMarkRead() {
  const evs = _notifLastEvents || [];
  if (evs.length) {
    const latest = evs[evs.length - 1].ts || '';
    if (latest > _notifSince) {
      _notifSince = latest;
      try { localStorage.setItem('tq_notif_since', _notifSince); } catch(e) {}
    }
  }
  const badge = document.getElementById('notif-badge');
  if (badge) badge.style.display = 'none';
}

let _notifLastEvents = [];
let _notifTypeFilter = 'all';   // all | alert | task
let _notifProjectFilter = '';   // '' = all projects
// ISO watermark of alert events already shown (toast + desktop notification). Kept in localStorage so a
// reload neither repeats them nor misses alerts that arrived while the page was closed. null = first visit:
// the first poll only sets the watermark.
let _notifToastSince = null;
try { _notifToastSince = localStorage.getItem('tq_notif_toast_since'); } catch (e) {}

export function tqSetNotifTypeFilter(kind) {
  _notifTypeFilter = kind === 'alert' || kind === 'task' ? kind : 'all';
  document.querySelectorAll('#notif-filter-bar [data-notif-type]').forEach(b => {
    b.classList.toggle('active', b.getAttribute('data-notif-type') === _notifTypeFilter);
  });
  _renderNotifPanel(_notifLastEvents);
}
export function tqSetNotifProjectFilter(el) {
  _notifProjectFilter = (el && el.value) ? String(el.value) : '';
  _renderNotifPanel(_notifLastEvents);
}
function _notifPassesFilters(ev) {
  const st = String(ev.status || '');
  const isAlert = st.startsWith('alert:');
  if (_notifTypeFilter === 'alert' && !isAlert) return false;
  if (_notifTypeFilter === 'task' && isAlert) return false;
  if (_notifProjectFilter) {
    const proj = String(ev.project || (ev.detail && ev.detail.project) || '');
    if (proj !== _notifProjectFilter) return false;
  }
  return true;
}
function _syncNotifProjectOptions(events) {
  const sel = document.getElementById('notif-proj-filter');
  if (!sel) return;
  const projs = new Set();
  (events || []).forEach(ev => {
    const p = ev.project || (ev.detail && ev.detail.project);
    if (p) projs.add(String(p));
  });
  const cur = _notifProjectFilter;
  const opts = ['<option value="">' + escHtml(i18n.t('notif.allProjects') || 'all projects') + '</option>'];
  [...projs].sort().forEach(p => {
    opts.push(`<option value="${escHtml(p)}"${p === cur ? ' selected' : ''}>${escHtml(p)}</option>`);
  });
  sel.innerHTML = opts.join('');
}

export function tqAlertDetailHtml(ev) {
  const d = ev.detail || {};
  const rows = [];
  const add = (k, v) => { if (v != null && v !== '') rows.push(`<div><span style="color:var(--muted)">${escHtml(k)}:</span> ${escHtml(String(v))}</div>`); };
  if (d.project && d.task_id) {
    rows.push(
      `<div><span style="color:var(--muted)">task:</span> `
      + `<span class="proj-tag" style="display:inline;${projStyle(d.project)}">${escHtml(d.project)}</span>`
      + `/<span class="task-id-mono">${escHtml(d.task_id)}</span></div>`
    );
  }
  add('error', d.error_label && d.error_code ? d.error_label + ' (' + d.error_code + ')' : (d.error_code || d.error_label));
  add('exit code', d.exit_code);
  add('stop reason', d.stop_reason);
  add('attempt', d.attempt != null ? d.attempt + (d.max_attempts ? '/' + d.max_attempts : '') : '');
  add('elapsed', d.elapsed_sec != null ? dur(d.elapsed_sec) : '');
  add('started', d.started_at ? fmt(d.started_at) : '');
  add('worker', d.worker);
  add('block reason', d.block_reason);
  add('run', d.run_id);
  if (d.failed_tasks && d.failed_tasks.length) add('failed tasks', d.failed_tasks.join(', '));
  if (d.last_hour_runs) add('runs in last hour', Object.entries(d.last_hour_runs).map(([k, v]) => k + ' ' + v).join(', '));
  const tail = (d.log_tail || []).length
    ? `<pre style="margin:6px 0 0;padding:6px 8px;background:var(--bg3);border-radius:4px;white-space:pre-wrap;font-size:11px">${escHtml(d.log_tail.join('\n'))}</pre>` : '';
  const open = (d.project && d.task_id)
    ? `<div style="margin-top:6px"><button type="button" class="cfg-btn" data-click="${tqAct([['tqOpenDrawer', String(d.project), String(d.task_id)]])}" data-click-stop>${escHtml(i18n.t('btn.detail') || 'Detail')}</button></div>` : '';
  return `<div style="font-size:12px;line-height:1.5">${rows.join('')}${tail}${open}</div>`;
}
export function tqToggleAlertDetail(el) {
  const d = el.querySelector('.alert-detail');
  if (d) d.hidden = !d.hidden;
}
export function _openAlertsTab() {
  const b = document.querySelector('[data-tab="alerts"]');
  if (b) tab('alerts', b);
}

// Browser notification for a new alert event. Only events newer than the first poll reach here,
// so a page reload does not re-fire old alerts; `tag` also lets the OS collapse duplicates.
function _desktopNotify(ev, title, body) {
  const N = typeof window !== 'undefined' ? window.Notification : undefined;
  if (typeof N !== 'function' || N.permission !== 'granted') return;
  try { if (tqInQuietHours(localStorage.getItem('tq_quiet_hours'), new Date())) return; } catch (_) {}
  try { new N(title, { body, tag: 'tq-' + String(ev.status || '') + '-' + String(ev.ts || '') }); } catch (_) {}
}

export function _fetchNotifications(showAll) {
  if (!showAll && typeof document !== 'undefined' && document.hidden) return;
  // While the panel is open the list keeps showing everything; the poll only feeds toasts and the badge.
  const listAll = showAll || _notifPanelOpen;
  const since = listAll ? '' : _notifSince;
  fetch('/api/notifications?limit=50' + (since ? '&since=' + encodeURIComponent(since) : ''))
    .then(r => r.json())
    .then(data => {
      const events = data.events || [];
      if (!showAll) {
        if (_notifToastSince !== null) {
          events.filter(e => (e.ts || '') > _notifToastSince).forEach(e => {
            const st = String(e.status || '');
            if (!st.startsWith('alert:')) return;
            const msg = e.description || (e.project + '/' + e.task_id + ' ' + st);
            const kind = (st === 'alert:any_failure' || st === 'alert:tq_stopped' || st === 'alert:task_frozen' || st === 'alert:run_anomaly')
              ? 'err' : 'info';
            const ttl = e.detail && e.detail.title;
            showToast((ttl ? ttl + '\n' : '') + msg, kind, 9000, _openAlertsTab);
            _desktopNotify(e, ttl || st, msg);
          });
        }
        let latest = _notifToastSince || '';
        events.forEach(e => {
          if ((e.ts || '') > latest) latest = e.ts;
        });
        _notifToastSince = latest || '';
        try { localStorage.setItem('tq_notif_toast_since', _notifToastSince); } catch (e) {}
      }
      _notifLastEvents = events;
      // Count unseen
      const unseen = _notifLastEvents.filter(e => (e.ts || '') > _notifSince).length;
      const badge = document.getElementById('notif-badge');
      if (badge) {
        badge.textContent = unseen > 99 ? '99+' : String(unseen || '');
        badge.style.display = unseen > 0 ? 'inline-block' : 'none';
      }
      if (_notifPanelOpen) _renderNotifPanel(_notifLastEvents);
    }).catch(() => {});
}

export function _renderNotifPanel(events) {
  const list = document.getElementById('notif-list');
  if (!list) return;
  _syncNotifProjectOptions(events);
  const filtered = (events || []).filter(_notifPassesFilters);
  if (!filtered.length) {
    list.innerHTML = `<div style="padding:16px;color:var(--muted);text-align:center">${i18n.t('notif.noNotifications')}</div>`;
    return;
  }
  const STATUS_ICON = {completed:'✓', failed:'✗', interrupted:'⚠', suspended:'⏸', stopped:'⏹', started:'▶'};
  const STATUS_CLR = STATUS_HEX;
  list.innerHTML = [...filtered].reverse().map(ev => {
    const st = String(ev.status || '');
    const isAlert = st.startsWith('alert:');
    const icon = isAlert ? '!' : (STATUS_ICON[ev.status] || '•');
    const clr = isAlert ? '#f59e0b' : (STATUS_CLR[ev.status] || '#6b7280');
    const unseen = (ev.ts || '') > _notifSince;
    const d = ev.ts ? new Date(ev.ts).toLocaleTimeString() : '';
    const label = isAlert
      ? `<strong>${escHtml((ev.detail && ev.detail.title) || st)}</strong><div style="white-space:pre-line;font-size:12px">${escHtml(ev.description || '')}</div>
         <div class="alert-detail" hidden>${tqAlertDetailHtml(ev)}</div>`
      : `<strong><span class="proj-tag" style="display:inline;${projStyle(ev.project||'')}">${escHtml(ev.project||'')}</span>/${escHtml(ev.task_id||'')}</strong>
      <span class="notif-status" style="color:${clr}">${escHtml(ev.status||'')}</span>`;
    const err = (!isAlert && ev.last_error_code)
      ? `<span class="notif-err" title="${escHtml(ev.last_error_code)}">${escHtml(friendlyErrorText(ev.last_error_code, ev.block_reason || ''))}</span>` : '';
    return `<div class="notif-row${unseen ? ' unseen' : ''}${isAlert ? ' clickable' : ''}" ${isAlert ? `data-click="${tqAct([['tqToggleAlertDetail', TQ_THIS]])}"` : ''}>
      <span class="notif-icon" style="color:${clr}">${icon}</span>
      <div class="notif-body">${label}${err}</div>
      <span class="notif-time">${d}</span>
    </div>`;
  }).join('');
}

// Close panel on outside click
if (typeof document !== 'undefined' && document.addEventListener) document.addEventListener('click', function(e) {
  if (!_notifPanelOpen) return;
  const panel = document.getElementById('notif-panel');
  const bell = document.getElementById('notif-bell');
  if (panel && !panel.contains(e.target) && bell && !bell.contains(e.target)) {
    _notifPanelOpen = false;
    panel.style.display = 'none';
  }
});
