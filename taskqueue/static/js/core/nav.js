// Navigation helpers: tab switching, header-count -> queue jumps, page size, restart-status labels and the
// log <pre> scroll-state capture used around re-renders. Other helpers (tqSyncHeaderMark, _syncUrl, setQueueFilter, ...)
// are app.js / other-module globals resolved at call time.
import { S } from './state.js';

const _QUEUE_PAGE_SIZE = 50;
const _QUEUE_PAGE_SIZE_MOB = 10;

export function _pageSize() { return window.innerWidth <= 768 ? _QUEUE_PAGE_SIZE_MOB : _QUEUE_PAGE_SIZE; }
/** "active" chip: every project except frozen/paused ones (queued waits stay visible). */
export function setQueueActive() {
  S.queueActiveOnly = true;
  setProject('all', { keepActive: true });
}

export function _captureLogScrollStates() {
  const states = {};
  document.querySelectorAll('pre').forEach(pre => {
    if (pre.id) {
      const isAtBottom = pre.scrollHeight - pre.clientHeight <= pre.scrollTop + 30;
      states[pre.id] = {
        scrollTop: pre.scrollTop,
        isAtBottom: isAtBottom
      };
    }
  });
  return states;
}

export function _restoreLogScrollStates(states) {
  for (const id in states) {
    const pre = document.getElementById(id);
    if (pre) {
      const state = states[id];
      if (state.isAtBottom) {
        pre.scrollTop = pre.scrollHeight;
      } else {
        pre.scrollTop = state.scrollTop;
      }
    }
  }
}

export function tab(name, btn) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('nav button').forEach(b => b.classList.remove('active'));
  document.getElementById(name).classList.add('active');
  btn.classList.add('active');
  // W3.8: persist last-visited tab for restore on next load.
  try { localStorage.setItem('tq_last_tab', name); } catch(_) {}
  if (name !== 'queue') tqCloseShortcuts();
  tqSyncHeaderMark();
  // W0.2: switching tabs triggers a fresh fetch so the 30 s interval worst-case
  // doesn't apply to the first view of a tab after switching.
  tqLoadTabBusy(name);
  if (!S.applyingUrl) _syncUrl({ replace: false });
}

/** Header count → queue filtered to that status. Clears search/tag/group so the list matches the count. */
export function jumpToStatus(status, opts) {
  _resetQueueSearchForFilter();
  if (S.tagFilter) {
    S.tagFilter = null;
    try { localStorage.setItem('tq_tag', ''); } catch (e) {}
    _updateTagChips();
  }
  if (S.groupFilter) {
    S.groupFilter = null;
    _updateGroupChips();
  }
  const queuePage = document.getElementById('queue');
  if (queuePage && !queuePage.classList.contains('active')) {
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('nav button').forEach(b => b.classList.remove('active'));
    queuePage.classList.add('active');
    const nav = document.querySelector('nav button[data-tab="queue"]');
    if (nav) nav.classList.add('active');
    try { localStorage.setItem('tq_last_tab', 'queue'); } catch (_) {}
    tqSyncHeaderMark();
  }
  const chip = document.querySelector('#queue-filter-bar .filter-chip[data-status="' + status + '"]');
  setQueueFilter(status, chip, opts);
}

const _RESTART_STATUSES = ['stopped', 'interrupted', 'suspended', 'cancelled'];
export function _isRestartStatus(status) { return _RESTART_STATUSES.includes(status); }
export function _requeueLabel(status) {
  return _isRestartStatus(status) ? i18n.t('btn.restart') : i18n.t('btn.retry');
}
