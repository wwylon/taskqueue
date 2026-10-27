// Keyboard shortcuts: help/shortcuts overlay toggles, queue-row navigation (j/k/a/Enter/r/s) and the global keydown handler.
// Uses app.js globals at call time (tab, openHelp/closeHelp, tqOpenDrawer, tqFindTask, refresh, ...).
import { S } from '../core/state.js';

export function tqQueueActive() {
  const q = document.getElementById('queue');
  return !!(q && q.classList.contains('active'));
}
export function tqCloseShortcuts() {
  const el = document.getElementById('tq-shortcuts');
  if (el) el.hidden = true;
}
export function tqToggleShortcuts() {
  const el = document.getElementById('tq-shortcuts');
  if (!el) return;
  el.hidden = !el.hidden;
}
export function tqHeaderMark() {
  if (tqQueueActive()) tqToggleShortcuts();
  else openHelp();
}
export function tqSyncHeaderMark() {
  const btn = document.getElementById('header-help-btn');
  if (!btn) return;
  const key = tqQueueActive() ? 'shortcuts.openTitle' : 'help.openTitle';
  btn.setAttribute('data-i18n-title', key);
  btn.title = (typeof i18n !== 'undefined' && i18n.t) ? i18n.t(key) : btn.title;
}
export function tqQueueRowEls() {
  return [...document.querySelectorAll('#queue-tbody tr[data-project]')];
}
export function tqHighlightedTask() {
  if (!S.kbdTaskKey) return null;
  const sep = S.kbdTaskKey.indexOf('\0');
  if (sep < 0) return null;
  return tqFindTask(S.kbdTaskKey.slice(0, sep), S.kbdTaskKey.slice(sep + 1));
}
export function tqMoveQueueRow(delta) {
  const rows = tqQueueRowEls();
  if (!rows.length) return;
  let i = rows.findIndex(r => _queueSelKey(r.getAttribute('data-project'), r.getAttribute('data-task')) === S.kbdTaskKey);
  if (i < 0) i = delta > 0 ? -1 : rows.length;
  i = Math.max(0, Math.min(rows.length - 1, i + delta));
  const row = rows[i];
  S.kbdTaskKey = _queueSelKey(row.getAttribute('data-project'), row.getAttribute('data-task'));
  rows.forEach(r => r.classList.toggle('qrow-kbd', r === row));
  row.scrollIntoView({ block: 'nearest' });
}
export function tqTypingTarget(el) {
  if (!el) return false;
  const tag = el.tagName;
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || el.isContentEditable;
}
// A modal-bg is "open" when it has the `open` class or an inline display other than none (yaml/resource modals).
function _modalOpen() {
  return Array.from(document.querySelectorAll('.modal-bg')).some(m =>
    m.classList.contains('open') || (m.style.display && m.style.display !== 'none'));
}
export function tqHandleKey(e) {
  if (e.key === 'Escape') {
    const help = document.getElementById('tq-help-modal');
    if (help && help.style.display !== 'none') { closeHelp(); return; }
    // Modals own Esc: never close the drawer/log panel/selection behind them.
    if (_modalOpen()) return;
    const shortcuts = document.getElementById('tq-shortcuts');
    if (shortcuts && !shortcuts.hidden) { shortcuts.hidden = true; return; }
    const drawer = document.getElementById('tq-drawer-back');
    if (drawer && !drawer.hidden) { tqCloseDrawer(); return; }
    const log = document.getElementById('tq-log-panel');
    if (log && !log.hidden) { tqCloseLog(); return; }
    if (S.queueSelection.size) tqClearSelection();
    return;
  }
  if (tqTypingTarget(e.target)) return;
  // Browser/OS shortcuts (Ctrl+R reload, Ctrl+S save, Cmd+A ...) must never trigger queue actions.
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  if (e.key === '?') {
    e.preventDefault();
    const help = document.getElementById('tq-help-modal');
    if (help && help.style.display !== 'none') { closeHelp(); return; }
    if (tqQueueActive()) tqToggleShortcuts();
    else openHelp();
    return;
  }
  if (e.key === '/') {
    e.preventDefault();
    const histOn = document.getElementById('history') && document.getElementById('history').classList.contains('active');
    if (histOn) {
      const hs = document.getElementById('history-search');
      if (hs) { hs.focus(); hs.select(); }
      return;
    }
    const qbtn = document.querySelector('nav button[data-tab="queue"]');
    if (qbtn) tab('queue', qbtn);
    const s = document.getElementById('queue-search');
    if (s) { s.focus(); s.select(); }
    return;
  }
  const queueOn = document.getElementById('queue') && document.getElementById('queue').classList.contains('active');
  if (!queueOn) return;
  if (e.key === 'j') { tqMoveQueueRow(1); return; }
  if (e.key === 'k') { tqMoveQueueRow(-1); return; }
  if (e.key === 'a') { tqToggleSelectVisible(true); return; }
  if (e.key === 'Enter') {
    // Enter on a focused button/link activates that control, not the highlighted row.
    if (e.target && e.target.closest && e.target.closest('button, a, summary, [role="button"]')) return;
    const t = tqHighlightedTask();
    if (t && t.task_id) { e.preventDefault(); tqOpenDrawer(t.project, t.task_id); }
    return;
  }
  if (e.key === 'r' || e.key === 's') {
    const t = tqHighlightedTask();
    if (!t || !t.task_id) return;
    e.preventDefault();
    if (e.key === 'r') {
      if (!['failed','interrupted','stopped','suspended','cancelled','completed'].includes(t.status)) return;
      if (!window.confirm(i18n.t('confirm.retryTask', { task: t.task_id }))) return;
      toastResult(_postJson(`/api/retry/${encodeURIComponent(t.project)}/${encodeURIComponent(t.task_id)}`),
        i18n.t('toast.retried'), refresh);
    } else if (t.status === 'running' || t.status === 'queued') {
      if (!window.confirm(i18n.t('confirm.stopTask', { task: t.task_id }))) return;
      toastResult(_postJson(`/api/stop/${encodeURIComponent(t.project)}/${encodeURIComponent(t.task_id)}`),
        i18n.t('toast.stopped'), refresh);
    }
  }
}
