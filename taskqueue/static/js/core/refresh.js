// Data refresh: /api/status polling, status pill/title/health indicators, offline + SSE indicators, live-update
// transport (SSE with polling heartbeat) and per-tab data loading. Uses app.js globals at call time
// (renderQueue, renderDashboard, _patchDashboardLive, _populateProjectSelect, ...). Refresh bookkeeping is module-local.
import { S } from '../core/state.js';
import { tqPerfEnabled, tqDominantTimingPhase } from './api.js';

export function _loadTabData(name) {
  if (name === 'queue' || name === 'history' || name === 'workers' || name === 'dashboard') return refresh();
  if (name === 'groups') return loadGroups();
  if (name === 'resources') return loadResources();
  if (name === 'usage') return loadUsage();
}

/** Load a tab's data with the loading indicator (tqBusy). */
export function tqLoadTabBusy(name) {
  return tqBusy(Promise.all([_loadTabData(name), tqLoadTab(name)]), name);
}

// ---- Data fetch ----

let _refreshInFlight = false;
let _refreshPending = false;   // a call arrived while in-flight; re-run once after
let _refreshFailStreak = 0;    // require consecutive failures before offline banner

// SSE must not hammer /api/dashboard on every data_version bump (daemon metrics writes).
const _QUEUE_REFRESH_MIN_MS = 15000;
let _lastQueueFullRefresh = 0;
let _queueRefreshTimer = null;

/** True when an SSE snapshot's data_version differs from the last one seen (or the server sends none); records it. */
export function _dataVersionChanged(dv) {
  if (dv != null && dv === S.lastDataVersion) return false;
  S.lastDataVersion = dv;
  return true;
}

/** Structural queue identity — ignores live elapsed/idle so SSE metrics do not force renderQueue. */
export function _queueStructuralKey(q, tasks) {
  const parts = (tasks || []).map(t =>
    [t.project, t.task_id, t.status, t.priority, t.run_id || '', t.enabled === false ? 0 : 1,
     t.last_error_code || '', t.block_reason || '', (t.missing_deps && t.missing_deps.length) || 0].join(':')
  );
  return String(q && q.total != null ? q.total : parts.length) + '#' + parts.join('|');
}

/** Merge SSE running[] into S.queue + patch progress DOM without a full table rebuild. */
export function _patchQueueLiveFromRunning(running) {
  if (!running || !running.length || !S.queue || !S.queue.length) return;
  const byKey = {};
  for (const t of running) byKey[t.project + '/' + t.task_id] = t;
  for (const t of S.queue) {
    const live = byKey[t.project + '/' + t.task_id];
    if (!live) continue;
    if (live.elapsed_sec != null) t.elapsed_sec = live.elapsed_sec;
    if (live.progress_pct != null) t.progress_pct = live.progress_pct;
    if (live.idle_sec != null) t.idle_sec = live.idle_sec;
    if (live.active_idle_sec != null) t.active_idle_sec = live.active_idle_sec;
    if (live.last_log_line != null) t.last_log_line = live.last_log_line;
    if (live.run_id != null) t.run_id = live.run_id;
    if (live.progress_health) t.progress_health = live.progress_health;
    const row = typeof qrowId === 'function' ? document.getElementById(qrowId(t.project, t.task_id)) : null;
    if (!row) continue;
    const pbar = row.querySelector('[data-q-pbar]');
    if (pbar && live.progress_pct != null) pbar.style.width = Math.min(100, live.progress_pct) + '%';
    const eta = row.querySelector('[data-q-eta]');
    if (eta && live.progress_pct != null) {
      const rem = (typeof _etaRemainingSec === 'function')
        ? _etaRemainingSec(t, live.elapsed_sec, live.progress_pct) : null;
      eta.textContent = rem != null
        ? (live.progress_pct.toFixed(0) + '% · ETA ' + dur(rem))
        : (live.progress_pct.toFixed(0) + '%');
    }
  }
}

function _scheduleQueueRefresh(delayMs, forceSoon) {
  if (document.hidden) return;
  if (forceSoon) {
    if (_queueRefreshTimer) { clearTimeout(_queueRefreshTimer); _queueRefreshTimer = null; }
    _queueRefreshTimer = setTimeout(() => {
      _queueRefreshTimer = null;
      _lastQueueFullRefresh = Date.now();
      refresh();
    }, Math.max(0, delayMs));
    return;
  }
  if (_queueRefreshTimer) return;
  const since = Date.now() - _lastQueueFullRefresh;
  const wait = Math.max(delayMs || 0, _QUEUE_REFRESH_MIN_MS - since);
  _queueRefreshTimer = setTimeout(() => {
    _queueRefreshTimer = null;
    _lastQueueFullRefresh = Date.now();
    refresh();
  }, Math.max(0, wait));
}

export function _updateStatusPill(s) {
  if (!s || typeof s !== 'object') return;
  const counts = s.status_counts;
  if (counts && typeof counts === 'object') {
    const setCount = (id, n, hideWhenZero = false) => {
      const el = document.getElementById(id);
      if (!el) return;
      const v = Number(n) || 0;
      el.textContent = String(v);
      if (hideWhenZero) {
        const btn = el.closest('button');
        if (btn) btn.hidden = v === 0;
      }
    };
    setCount('sc-running', counts.running);
    setCount('sc-queued', counts.queued);
    setCount('sc-completed', counts.completed);
    setCount('sc-failed', counts.failed);
    setCount('sc-interrupted', counts.interrupted, true);
    setCount('sc-suspended', counts.suspended, true);
    setCount('sc-stopped', counts.stopped, true);
  }
  _updatePageTitle(s);
  _updateHealth(s.health);
  const badge = document.getElementById('nav-alerts-badge');
  if (badge && s.alert_firing != null) {
    const n = Number(s.alert_firing) || 0;
    badge.hidden = n <= 0;
    badge.textContent = String(n);
  }
  const n = (s.running || []).length;
  const pill = document.getElementById('daemon-pill');
  if (pill) {
    pill.className = 'status-pill ' + (n > 0 ? 'pill-ok' : 'pill-idle');
    pill.textContent = n > 0 ? i18n.t('status.nRunning', {n}) : i18n.t('status.idle');
  }
  const cfg = s.config || S.cfg || {};
  const maxTasks = parseInt(cfg.max_running_tasks) || 0;
  const overLimit = document.getElementById('over-limit-warn');
  if (overLimit) overLimit.style.display = (maxTasks > 0 && n > maxTasks) ? '' : 'none';
  const vbadge = document.getElementById('version-badge');
  if (vbadge && s.version) {
    const sha = s.git_sha && s.git_sha !== 'unknown' ? ` · ${s.git_sha}` : '';
    vbadge.textContent = `tq v${s.version} · db v${s.schema_version}${sha}`;
  }
}

/** Browser tab title: tq {running}/{failed}/{queued} [project]. */
export function _updatePageTitle(s) {
  const c = (s && s.status_counts) || {};
  const running = Number(c.running) || 0;
  const failed = Number(c.failed) || 0;
  const queued = c.queued != null
    ? (Number(c.queued) || 0)
    : (Number(s && s.queued_count) || 0);
  let title = 'tq ' + running + '/' + failed + '/' + queued;
  const h = s && s.health;
  if (h && h.level !== 'green' && h.icons) title = h.icons.level[h.level] + ' ' + title;
  const proj = (S.selectedProject && S.selectedProject !== 'all') ? S.selectedProject : '';
  if (proj) title += ' ' + proj;
  document.title = title;
}
/** Queue health pill, favicon dot and Acknowledge button (taskqueue/health.py). */
const _HEALTH_COLORS = {red: '#ef4444', amber: '#f59e0b', green: '#22c55e'};
let _health = null;
export function _updateHealth(h) {
  const pill = document.getElementById('health-pill');
  if (!pill || !h || !h.level) return;
  _health = h;
  const icons = h.icons || {level: {}, parts: {}};
  // Blocked projects (frozen) are shown in the queue's "active" view, not in the header pill.
  const parts = (h.parts || []).filter(p => p.kind !== 'frozen');
  pill.hidden = false;
  pill.className = 'health-pill health-' + h.level;
  pill.textContent = (icons.level[h.level] || '') + ' ' + (parts.length
    ? parts.map(p => (icons.parts[p.kind] || '') + (p.kind === 'daemon_down' ? '' : p.count)).join(' ')
    : i18n.t('health.ok'));
  const lines = parts.map(p => (icons.parts[p.kind] || '') + ' ' + i18n.t('health.part.' + p.kind, {n: p.count}));
  const keys = [].concat(h.new_failed || [], h.interrupted || []);
  pill.title = (lines.length ? lines.join('\n') : i18n.t('health.ok')) + (keys.length ? '\n\n' + keys.join('\n') : '');
  const ack = document.getElementById('health-ack');
  if (ack) ack.hidden = !(h.counts && h.counts.new_failed);
  _setHealthFavicon(h.level);
}
export function _setHealthFavicon(level) {
  if (_setHealthFavicon._last === level) return;
  _setHealthFavicon._last = level;
  const link = document.querySelector('link[rel="icon"]');
  if (!link) return;
  link.href = 'data:image/svg+xml,' + encodeURIComponent(
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><text y='26' font-size='28'>📋</text>"
    + "<circle cx='25' cy='7' r='7' fill='" + _HEALTH_COLORS[level] + "' stroke='#fff' stroke-width='1.5'/></svg>");
}
window.tqHealthClick = function () {
  const c = (_health && _health.counts) || {};
  // Failures: the "active" view (no frozen/paused projects), narrowed to what the badge counts.
  if (c.new_failed || c.failed) {
    setQueueActive();
    jumpToStatus('failed', { newFailed: !!c.new_failed });
  }
  else if (c.interrupted) jumpToStatus('interrupted');
  else if (c.frozen) jumpToStatus('running');
};
window.tqHealthAck = async function () {
  const r = await fetch('/api/health/ack', {method: 'POST'});
  const d = await r.json().catch(() => ({}));
  if (d && d.health) _updateHealth(d.health);
  refresh();
};

window.tqUpdatePageTitle = function () {
  if (S.status) _updatePageTitle(S.status);
};

/** Project scope for header counts / dashboard status (matches refresh()). */
export function _statusScopeProject() {
  if (S.selectedProject && S.selectedProject !== 'all') return S.selectedProject;
  if (S.queueProjectFilter && S.queueProjectFilter !== 'all') return S.queueProjectFilter;
  return '';
}

/**
 * SSE /api/events is always fleet-wide. When a project chip is selected, keep
 * status_counts/queued_count from the last scoped /api/status refresh and filter
 * running[] — otherwise the top bar and dashboard flash between all vs project.
 */
export function _scopeStatusPayload(s) {
  const proj = _statusScopeProject();
  if (!proj || !s || typeof s !== 'object') return s;
  const prev = S.status || {};
  const out = Object.assign({}, s, {
    running: (s.running || []).filter(t => t.project === proj),
  });
  if (prev.status_counts && typeof prev.status_counts === 'object') {
    out.status_counts = prev.status_counts;
  }
  if (prev.queued_count != null) {
    out.queued_count = prev.queued_count;
  } else if (out.status_counts && out.status_counts.queued != null) {
    out.queued_count = Number(out.status_counts.queued) || 0;
  }
  return out;
}

// Expanded log/history panes are deleted on collapse, but rows that leave the page while open
// keep their entry. Keep the newest few (insertion order) so a long session cannot grow these.
const _UI_MAP_CAP = 40;
function _capUiMaps() {
  for (const m of [S.expandedLogs, S.expandedHistory, S.logSearch]) {
    if (!m) continue;
    const keys = Object.keys(m);
    for (let i = 0; i < keys.length - _UI_MAP_CAP; i++) delete m[keys[i]];
  }
}

export async function refresh() {
  if (_refreshInFlight) { _refreshPending = true; return; }
  _refreshInFlight = true;
  try {
    const proj = _statusScopeProject();
    const parts = [];
    if (proj) parts.push(`project=${encodeURIComponent(proj)}`);
    if (S.queueStatusFilter !== 'all') parts.push(`status=${encodeURIComponent(S.queueStatusFilter)}`);
    if (!proj && S.queueActiveOnly) parts.push('hide_blocked=1');
    if (S.queueNewFailedOnly && S.queueStatusFilter === 'failed') parts.push('new_failed=1');
    if (S.tagFilter) parts.push(`tag=${encodeURIComponent(S.tagFilter)}`);
    if (S.groupFilter) parts.push(`group=${encodeURIComponent(S.groupFilter)}`);
    // R8: mine scope → started_by (operator from prefs / TQ_OPERATOR equivalent).
    if (S.mineOnly && S.operator) parts.push(`started_by=${encodeURIComponent(S.operator)}`);
    // Determine active tab for /api/dashboard so it bundles the right payload.
    const _histEl = document.getElementById('history');
    const _wrkEl = document.getElementById('workers');
    const dashTab = (_histEl && _histEl.classList.contains('active')) ? 'history'
      : (_wrkEl && _wrkEl.classList.contains('active')) ? 'workers' : 'queue';
    // The server reads the first value of each param, so send queue OR history
    // search/limit/offset — never both (a duplicate made History ignore its own).
    if (dashTab === 'history') {
      const _hSearch = (document.getElementById('history-search') || {}).value || '';
      if (_hSearch.trim()) parts.push(`search=${encodeURIComponent(_hSearch.trim())}`);
      parts.push(`limit=${S.historyLimit}`);
    } else {
      const _qSearch = (document.getElementById('queue-search') || {}).value || '';
      if (_qSearch.trim()) parts.push(`search=${encodeURIComponent(_qSearch.trim())}`);
      parts.push(`sort=${encodeURIComponent(S.queueSort.field)}`);
      parts.push(`sort_dir=${S.queueSort.dir > 0 ? 'asc' : 'desc'}`);
      parts.push(`limit=${_pageSize()}`);
      parts.push(`offset=${S.queuePage * _pageSize()}`);
    }
    parts.push(`tab=${dashTab}`);
    const _perfOn = tqPerfEnabled();
    if (_perfOn) parts.push('timing=1');

    const dashUrl = '/api/dashboard?' + parts.join('&');
    const { data: resp, etag: newEtag, notModified, wallMs, serverTiming } = await _fetchJsonEtag(dashUrl, 15000, S.dashboardEtagUrl === dashUrl && !S.forceQueueRender ? S.dashboardEtag : '');

    function _tqPerfNote(base) {
      if (!_perfOn) return base;
      const dom = tqDominantTimingPhase(serverTiming || '');
      const bit = (wallMs != null ? wallMs + 'ms' : '') + (dom ? ' · ' + dom : '');
      return bit ? (base + ' · ' + bit) : base;
    }

    if (notModified) {
      // Nothing changed — update timestamps and bail early.
      _refreshFailStreak = 0;
      S.lastRefreshTime = Date.now();
      S.sseState = 'live';
      _updateSseIndicator();
      const el = document.getElementById('last-updated');
      if (el) el.textContent = _tqPerfNote(i18n.t('status.updatedJustNow'));
      const resolvers = S.refreshResolvers.splice(0);
      resolvers.forEach(fn => fn());
      return;
    }

    if (!resp || typeof resp !== 'object' || !resp.status) {
      throw new Error('dashboard unreachable');
    }

    S.dashboardEtag = newEtag || '';
    S.dashboardEtagUrl = dashUrl;
    const s = resp.status;
    const q = resp.queue || {};
    const h = resp.history || {};
    const w = resp.workers || null;
    const tg = resp.tags || {};
    const pr = resp.project_roots || null;

    _refreshFailStreak = 0;
    S.status = s;
    S.cfg = s.config || {};
    _updateStatusPill(s);
    _setOffline(false);
    const dotEarly = document.getElementById('live-dot');
    if (dotEarly) dotEarly.classList.add('live');
    let projectsChanged = false;
    if (s.projects && JSON.stringify(s.projects) !== JSON.stringify(S.projects)) {
      S.projects = s.projects;
      _populateProjectSelect(S.projects);
      projectsChanged = true;
    }
    if (Array.isArray(resp.my_projects)) {
      const mpStr = JSON.stringify(resp.my_projects);
      if (mpStr !== JSON.stringify(S.myProjects)) {
        S.myProjects = resp.my_projects;
        projectsChanged = true;
      }
    } else if (!S.mineOnly) {
      S.myProjects = [];
    }
    if (projectsChanged && S.projects) _populateProjectChips(S.projects);

    if (tg && tg.tags) { S.allTags = tg.tags || []; S.allTagProjects = tg.tag_projects || {}; }
    if (pr && typeof pr === 'object') S.projectRoots = pr;

    const tasks = Array.isArray(q) ? q : (q.tasks || []);
    // Only the active tab's section is bundled; keep the other sections' last state.
    const hasQueue = resp.queue != null;
    const hasHistory = resp.history != null;
    // Cheap structural key (not full JSON.stringify) — live elapsed/progress ignored.
    const queueStr = hasQueue ? _queueStructuralKey(q, tasks) : S.prevQueueStr;
    const historyStr = hasHistory ? JSON.stringify(h) : S.prevHistoryStr;
    const workersStr = w != null ? JSON.stringify(w) : S.prevWorkersStr;

    const runningIdentity = _runningIdentity(s.running, s.config || S.cfg);
    const gaugeStruct = _gaugeStructureKey(s, s.config || S.cfg);

    let queueChanged = hasQueue && queueStr !== S.prevQueueStr;
    let historyChanged = hasHistory && historyStr !== S.prevHistoryStr;
    let workersChanged = w != null && workersStr !== S.prevWorkersStr;
    let dashStructureChanged = runningIdentity !== S.prevRunningStr
      || gaugeStruct !== S.prevGaugeStructStr
      || !document.querySelector('#gauges [data-gauge]');

    if (hasQueue) {
      S.queue = Array.isArray(q) ? q : (q.tasks || []); S.queueTotal = q.total || S.queue.length;
      S.brokenDeps = Array.isArray(q) ? [] : (q.broken_deps || []);
      _updateBrokenDepsBanner();
    }
    if (hasHistory) { S.history = Array.isArray(h) ? h : (h.runs || []); S.historyTotal = h.total || S.history.length; }
    if (w != null) S.workers = w;
    S.prevQueueStr = queueStr;
    S.prevHistoryStr = historyStr;
    if (w != null) S.prevWorkersStr = workersStr;
    S.prevRunningStr = runningIdentity;
    S.prevGaugeStructStr = gaugeStruct;
    _lastQueueFullRefresh = Date.now();
    _populateGroupChips();
    if (workersChanged) {
      _populateWorkerSelect(S.workers);
    }
    const scrollStates = _captureLogScrollStates();

    if (dashStructureChanged) {
      try { renderDashboard(); } catch(e2) { console.error('[tq] renderDashboard threw:', e2); throw new Error('renderDashboard: ' + e2.message); }
    } else {
      _patchDashboardLive();
    }

    if (queueChanged || S.forceQueueRender) {
      S.forceQueueRender = false;
      try {
        renderQueue();
        for (const [preId, state] of Object.entries(S.logSearch)) {
          if (state && state.query && document.getElementById(preId)) {
            _applyLogSearch(preId, state.query, state.current || 0);
            const inp = document.getElementById('log-search-inp-' + preId);
            if (inp) inp.value = state.query;
          }
        }
      } catch(e2) { console.error('[tq] renderQueue threw:', e2); throw new Error('renderQueue: ' + e2.message); }
    } else if (hasQueue) {
      _patchQueueLiveFromRunning(s.running);
    }
    _updateTagChips();

    if (historyChanged) {
      try { renderHistory(); } catch(e2) { console.error('[tq] renderHistory threw:', e2); throw new Error('renderHistory: ' + e2.message); }
    }

    if (workersChanged) {
      try { renderWorkers(); } catch(e2) { console.error('[tq] renderWorkers threw:', e2); throw new Error('renderWorkers: ' + e2.message); }
    }

    _restoreLogScrollStates(scrollStates);

    // Auto-fetch new log content for any running task whose log is currently expanded
    for (const t of (s.running || [])) {
      // W3.2: panel-scoped refresh for dashboard
      if (t.run_id && S.expandedLogs[`d:${t.run_id}`]) {
        _fetchLog(t.run_id, `d-log-pre-${t.run_id}`);
      }
    }

    S.lastRefreshTime = Date.now();
    _capUiMaps();
    tqRefreshActivePage();
    S.sseState = 'live';  // I1
    _updateSseIndicator();
    const el = document.getElementById('last-updated');
    if (el) el.textContent = _tqPerfNote(i18n.t('status.updatedJustNow'));
    // Resolve B4/setProjectAndWait resolvers
    const resolvers = S.refreshResolvers.splice(0);
    resolvers.forEach(fn => fn());
  } catch(e) {
    console.error('[tq] refresh() threw:', e, e && e.stack);
    // A render threw after the change-detection keys were stored: reset them so the next refresh re-renders instead of freezing.
    S.prevQueueStr = S.prevHistoryStr = S.prevWorkersStr = S.prevRunningStr = S.prevGaugeStructStr = '';
    S.dashboardEtag = '';  // a 304 next time would skip the re-render and freeze the UI
    _refreshFailStreak += 1;
    // Do not reset the status pill on a slow/failed queue fetch — SSE keeps it accurate.
    if (_refreshFailStreak >= 3) {
      _setOffline(true);
      const pill = document.getElementById('daemon-pill');
      if (pill) {
        pill.className = 'status-pill pill-fail';
        pill.textContent = i18n.t('status.connecting');
      }
      const dot = document.getElementById('live-dot');
      if (dot) dot.classList.remove('live');
      S.sseState = 'error';
      _updateSseIndicator();
    }
  } finally {
    _refreshInFlight = false;
    if (_refreshPending) { _refreshPending = false; refresh(); }
  }
}

let _isOffline = false;
let _a11yLast = '';
function _a11yAnnounce(msg) {
  const live = document.getElementById('tq-a11y-live');
  if (!live || !msg || msg === _a11yLast) return;
  _a11yLast = msg;
  // Direct set (no rAF): avoids 0ms timer noise in tests and duplicate announces.
  live.textContent = msg;
}
export function _setOffline(offline) {
  if (offline === _isOffline) return;
  _isOffline = offline;
  const banner = document.getElementById('offline-banner');
  const header = document.getElementById('main-header');
  if (banner) banner.style.display = offline ? '' : 'none';
  if (header) header.style.marginTop = offline ? '36px' : '0';
  _a11yAnnounce(offline
    ? (i18n.t('header.offlineBanner') || 'Daemon offline')
    : (i18n.t('status.online') || 'Daemon online'));
  // Do not call refresh() here — caller already refreshed / is about to.
}

// I1/I2: SSE connection indicator
export function _updateSseIndicator() {
  const dot = document.getElementById('sse-dot');
  if (!dot) return;
  let title;
  if (S.sseState === 'live') {
    dot.style.background = 'var(--ok)'; title = i18n.t('sse.live');
  } else if (S.sseState === 'polling') {
    dot.style.background = '#f59e0b'; title = i18n.t('sse.polling');
  } else if (S.sseState === 'error') {
    dot.style.background = 'var(--fail)'; title = i18n.t('sse.error');
  } else {
    dot.style.background = 'var(--muted)'; title = i18n.t('sse.connecting');
  }
  // Announce only on change (deduped in _a11yAnnounce).
  if (dot.title !== title) _a11yAnnounce(title);
  dot.title = title;
}

/** Connect the live event stream and start the polling heartbeat. Called once from app.js boot. */
export function startLiveUpdates() {
  // I-10: "X seconds ago" counter (started here, not at import, so tests can load the module)
  setInterval(function() {
    if (!S.lastRefreshTime) return;
    const sec = Math.round((Date.now() - S.lastRefreshTime) / 1000);
    const el = document.getElementById('last-updated');
    if (el) el.textContent = sec <= 2 ? i18n.t('status.updatedJustNow') : i18n.t('status.updatedAgo', {n: sec});
    // I1: flag error after 30s with no update
    if (sec > 30 && S.sseState !== 'error') { S.sseState = 'error'; _updateSseIndicator(); }
    if (sec > 30) _setHealthFavicon('red');  // daemon unreachable
  }, 1000);
  let _sse = null;
  let _sseActive = false;
  function _onSSEMessage(s) {
    if (!s || typeof s !== 'object' || !('running' in s)) return;
    s = _scopeStatusPayload(s);
    // Update status globals used by renderDashboard
    S.status = s;
    S.cfg = s.config || {};
    if (s.projects && JSON.stringify(s.projects) !== JSON.stringify(S.projects)) {
      S.projects = s.projects;
      _populateProjectSelect(S.projects);
      _populateProjectChips(S.projects);
    }
    const prevRunIdentity = S.prevRunningStr;
    const runIdentity = _runningIdentity(s.running, s.config || S.cfg);
    const gaugeStruct = _gaugeStructureKey(s, s.config || S.cfg);
    const structural = runIdentity !== prevRunIdentity;
    if (structural || gaugeStruct !== S.prevGaugeStructStr
        || !document.querySelector('#gauges [data-gauge]')) {
      S.prevRunningStr = runIdentity;
      S.prevGaugeStructStr = gaugeStruct;
      renderDashboard();
    } else {
      _patchDashboardLive();
    }
    // Live queue progress from SSE — no full /api/dashboard.
    _patchQueueLiveFromRunning(s.running);
    S.lastRefreshTime = Date.now();
    _refreshFailStreak = 0;
    S.sseState = 'live'; _updateSseIndicator();
    _setOffline(false);
    const dot = document.getElementById('live-dot');
    if (dot) dot.classList.add('live');
    _updateStatusPill(s);
    for (const t of (s.running || [])) {
      // W3.2: only refresh panel if that panel's log is expanded
      if (t.run_id && S.expandedLogs[`d:${t.run_id}`]) { _fetchLog(t.run_id, `d-log-pre-${t.run_id}`); }
    }
    // Full queue/history refetch: soon on membership change; else at most every 15s on data_version.
    if (_dataVersionChanged(s.data_version)) {
      if (structural) _scheduleQueueRefresh(400, true);
      else _scheduleQueueRefresh(1000, false);
    }
  }
  let _sseRetryTimer = null;
  // Single connection: close any previous stream and pending retry before opening one.
  function startSSE() {
    if (_sseRetryTimer) { clearTimeout(_sseRetryTimer); _sseRetryTimer = null; }
    if (_sse) { _sse.onopen = _sse.onmessage = _sse.onerror = null; _sse.close(); _sse = null; }
    try {
      // W1.6: EventSource cannot send headers; tqEventSource uses a short-lived ticket.
      const es = tqEventSource('/api/events');
      _sse = es;
      es.onopen = function() { _sseActive = true; S.sseState = 'live'; _updateSseIndicator(); };
      es.onmessage = function(ev) {
        try { _onSSEMessage(JSON.parse(ev.data)); } catch(e) {}
      };
      // Named transition events → immediate structural queue refresh (task started/stopped).
      es.addEventListener('transition', function() {
        _scheduleQueueRefresh(400, true);
      });
      es.onerror = function() {
        if (_sse !== es) return;
        _sseActive = false; S.sseState = 'error'; _updateSseIndicator();
        es.close(); _sse = null;
        _sseRetryTimer = setTimeout(startSSE, 5000);
      };
    } catch(e) {
      S.sseState = 'polling'; _updateSseIndicator();
    }
  }
  // Heartbeat: when SSE is live, soft fallback only every 120s (F-132).
  // When SSE is down, poll every 5s as the sole update source.
  (async function heartbeat() {
    while (true) {
      const delay = _sseActive ? 120000 : 5000;
      await new Promise(r => setTimeout(r, delay));
      if (document.hidden) continue;
      if (!_sseActive || typeof EventSource === 'undefined') {
        await refresh();
      } else {
        // Soft fallback while SSE open — rare safety net (was every 30s).
        await refresh();
      }
    }
  })();
  if (typeof EventSource !== 'undefined') {
    startSSE();
  } else {
    S.sseState = 'polling'; _updateSseIndicator();
  }
  // W3.1: resume immediately when tab becomes visible again after being hidden
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
      refresh();
      if (!_sse && !_sseRetryTimer && typeof EventSource !== 'undefined') startSSE();
    }
  });
}
