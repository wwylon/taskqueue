// Helpers escHtml/jsq/taskKey/fmt/dur/_appendLog/_fetchJson/_fetchJsonEtag/tqEventSource and the fetch
// auth wrapper live in static/js/ (ES modules) and are exposed on window by js/main.js, which loads first.
const S = window.tqState;  // shared UI state, defined in js/core/state.js (S.queue was _queue, etc.)
S.historyLimit = S.historyPageSize;

// Restore filters from the URL first, then localStorage, before the first fetch.
(function() {
  S.bootUi = _mergedUiState();
  _applyUiVars(S.bootUi);
})();

// v3 shell: theme, font, preferences popup
tqSyncPrefs();

document.addEventListener('keydown', tqHandleKey);

// Restore the page from ?page= (else the last tab). Write a canonical URL.
(function() {
  const name = _activateTabFromUrl((S.bootUi && S.bootUi.tab) || 'queue');
  tqLoadTabBusy(name);
  _syncUrl({ replace: true });
})();

window.addEventListener('popstate', function() {
  const fromUrl = _readUiUrl();
  S.applyingUrl = true;
  let name = 'queue';
  try {
    _applyUiVars({
      project: fromUrl.project || 'all',
      status: fromUrl.status || 'all',
      q: Object.prototype.hasOwnProperty.call(fromUrl, 'q') ? fromUrl.q : '',
      group: fromUrl.group || null,
      tag: fromUrl.tag || null,
      doc: fromUrl.doc || null,
    });
    name = _activateTabFromUrl(fromUrl.tab || 'queue');
  } finally {
    S.applyingUrl = false;
  }
  tqLoadTabBusy(name);
  _syncUrl({ replace: true });
});

// I-16: Use SSE for live running-task updates; fall back to polling
startLiveUpdates();

// Load usage when that tab is first activated
const _origTab = window.tab;
window.tab = function(name, btn) {
  _origTab(name, btn);
  if (name === 'usage') loadUsage();
};

// Close picker on backdrop click
const _mobPicker = document.getElementById('mob-task-picker');
if (_mobPicker) _mobPicker.addEventListener('click', function(e) {
  if (e.target === this) closeMobTaskPicker();
});

tqLoadSavedFilters();
tqApplyQueueColumns();
const _queueRoot = document.getElementById('queue');
if (_queueRoot) {
  _queueRoot.addEventListener('change', function (e) {
    const cb = e.target.closest && e.target.closest('.q-row-check');
    if (!cb) return;
    tqSetRowSelected(cb.getAttribute('data-project'), cb.getAttribute('data-task'), cb.checked);
  });
}
document.addEventListener('click', function (e) {
  const pop = document.getElementById('saved-filters-pop');
  const btn = document.getElementById('saved-filters-btn');
  if (!pop || pop.hidden) return;
  if (pop.contains(e.target) || (btn && btn.contains(e.target))) return;
  pop.hidden = true;
});

// Poll notifications every 30s (single-flight: skip if a prior poll is still in flight).
(function () {
  let _notifInFlight = false;
  async function _pollNotif() {
    if (_notifInFlight) return;
    _notifInFlight = true;
    try { await _fetchNotifications(false); }
    finally { _notifInFlight = false; }
  }
  _pollNotif();
  setInterval(_pollNotif, 30000);
})();

// Offline shell: register the service worker (static assets only) and show a banner while offline.
(function () {
  if ('serviceWorker' in navigator && location.protocol !== 'file:') {
    navigator.serviceWorker.register('/sw.js').catch(() => {});
  }
  function sync() {
    let b = document.getElementById('tq-offline-banner');
    if (navigator.onLine) { if (b) b.remove(); return; }
    if (b) return;
    b = document.createElement('div');
    b.id = 'tq-offline-banner';
    b.setAttribute('role', 'status');
    b.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:9999;padding:4px 12px;text-align:center;'
      + 'background:#b45309;color:#fff;font-size:13px';
    b.textContent = i18n.t('pwa.offline');
    document.body.appendChild(b);
  }
  window.addEventListener('online', sync);
  window.addEventListener('offline', sync);
  sync();
})();
