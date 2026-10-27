// Alerts tab: system alert toggles plus alert history. Uses app.js globals at call time: tqAct, TQ_THIS,
// i18n, showToast, refresh, tqAlertDetailHtml, _sendJson, _showPageEmpty.
import { S } from '../core/state.js';
import { escHtml, fmt } from '../util/format.js';
import { fetchJson } from '../core/api.js';
import { projStyle } from '../util/projcolor.js';

export const SYSTEM_ALERT_ORDER = ['any_failure', 'all_done', 'task_frozen', 'tq_stopped', 'run_anomaly'];

export function tqPatchSystemAlert(key, field, checked) {
  const on = checked && typeof checked === 'object' ? checked.checked : checked;
  const body = {};
  body[field] = on ? 1 : 0;
  _sendJson('PATCH', '/api/alerts/system/' + encodeURIComponent(key), body)
    .then(() => { loadAlerts(); refresh(); })
    .catch(err => {
      // Server refused: put the checkbox back so the UI does not show a state that was not saved.
      if (checked && typeof checked === 'object' && 'checked' in checked) checked.checked = !on;
      showToast(String(err.message || err), 'err');
    });
}

export function tqRequestNotifPermission() {
  if (!('Notification' in window)) { showToast('Browser notifications not supported', 'err'); return; }
  Notification.requestPermission().then(p => {
    showToast(p === 'granted' ? (i18n.t('alerts.notifGranted') || 'Notifications enabled') : (i18n.t('alerts.notifDenied') || 'Permission denied'), p === 'granted' ? 'ok' : 'err');
    loadAlerts();
  });
}

export async function loadAlerts() {
  const [data, histData] = await Promise.all([
    fetchJson('/api/alerts/system', 8000, true),
    fetchJson('/api/notifications?kind=alert&limit=100', 8000, true),
  ]);
  if (!data) return false;
  S.systemAlerts = data.alerts || [];
  const hist = ((histData && histData.events) || []).slice().reverse();   // newest first
  const lastByKey = {};
  hist.forEach(ev => { const k = String(ev.status || '').replace('alert:', ''); if (!lastByKey[k]) lastByKey[k] = ev; });
  const tb = document.getElementById('alerts-tbody');
  if (!tb) return;
  const byKey = {};
  S.systemAlerts.forEach(a => { byKey[a.key] = a; });
  const rows = SYSTEM_ALERT_ORDER.map(k => byKey[k]).filter(Boolean);

  // Desktop notifications fire from the global notification poll (components/notifications.js),
  // once per new alert event, whichever tab is open.

  // permission prompt banner
  const notifBanner = document.getElementById('alerts-notif-banner');
  if (notifBanner) {
    const perm = 'Notification' in window ? Notification.permission : 'unsupported';
    notifBanner.hidden = perm === 'granted' || perm === 'unsupported';
  }

  _showPageEmpty('alerts-empty', 'alerts-wrap', rows.length === 0);
  tb.innerHTML = rows.map(a => {
    const title = escHtml(i18n.t('alerts.sys.' + a.key));
    const desc = escHtml(i18n.t('alerts.sys.' + a.key + '.desc'));
    const key = escHtml(a.key);
    return `<tr>
      <td><div class="alert-sys-title">${title}</div><div class="alert-sys-desc">${desc}</div></td>
      <td><input type="checkbox" ${a.enabled ? 'checked' : ''}
        data-change="${tqAct([['tqPatchSystemAlert', key, 'enabled', TQ_THIS]])}"
        aria-label="${escHtml(i18n.t('alerts.enabled'))}"></td>
      <td><input type="checkbox" ${a.windows_toast ? 'checked' : ''}
        data-change="${tqAct([['tqPatchSystemAlert', key, 'windows_toast', TQ_THIS]])}"
        aria-label="${escHtml(i18n.t('alerts.windowsToast'))}"></td>
      <td>${a.firing ? escHtml(i18n.t('alerts.firing')) : '—'}</td>
      <td>${escHtml(String(a.fire_count != null ? a.fire_count : 0))}</td>
      <td>${a.last_fired_at ? escHtml(fmt(a.last_fired_at)) : '—'}${lastByKey[a.key] ? `<div class="alert-sys-desc" style="white-space:pre-line">${escHtml(String(lastByKey[a.key].description || '').split('\n')[0])}</div>` : ''}</td>
    </tr>`;
  }).join('');
  const hb = document.getElementById('alerts-hist-tbody');
  if (hb) {
    // Keep expanded history rows expanded across the periodic re-render.
    const openTs = new Set([...(hb.querySelectorAll ? hb.querySelectorAll('tr[data-ts]') : [])]
      .filter(tr => { const d = tr.querySelector('.alert-detail'); return d && !d.hidden; })
      .map(tr => tr.getAttribute('data-ts')));
    _showPageEmpty('alerts-hist-empty', 'alerts-hist-wrap', hist.length === 0);
    hb.innerHTML = hist.map(ev => {
      const k = String(ev.status || '').replace('alert:', '');
      const d = ev.detail || {};
      const taskHtml = (d.project && d.task_id)
        ? `<span class="proj-tag" style="display:inline;${projStyle(d.project)}">${escHtml(d.project)}</span>/<span class="task-id-mono">${escHtml(d.task_id)}</span>`
        : '';
      const lines = String(ev.description || '').split('\n');
      const isOpen = openTs.has(String(ev.ts || ''));
      return `<tr style="cursor:pointer" data-ts="${escHtml(String(ev.ts || ''))}" data-click="${tqAct([['tqToggleAlertDetail', TQ_THIS]])}">
        <td>${escHtml(fmt(ev.ts))}</td>
        <td>${escHtml(i18n.t('alerts.sys.' + k) || k)}</td>
        <td>${taskHtml}</td>
        <td><div>${escHtml(lines[0])}</div>${lines.slice(1).map(l => `<div style="color:var(--text2);font-size:12px">${escHtml(l)}</div>`).join('')}
          <div class="alert-detail"${isOpen ? '' : ' hidden'} style="margin-top:6px">${tqAlertDetailHtml(ev)}</div></td>
      </tr>`;
    }).join('');
  }
}
