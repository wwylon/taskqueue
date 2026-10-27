// Page loading plumbing: header project filter, per-tab loader dispatch, the single-flight back-off loop (tqLoop),
// the operator-started busy indicator (tqBusy) and small data-<event> delegation targets.
// Tab loaders (loadDeps, loadAudit, ...) and refresh/i18n are app.js / other-module globals resolved at call time.
import { S } from '../core/state.js';
import { escHtml } from '../util/format.js';

export function tqHeaderProject() {
  return (S.selectedProject && S.selectedProject !== 'all') ? S.selectedProject : '';
}
export function tqProjectQuery() {
  const p = tqHeaderProject();
  return p ? ('?project=' + encodeURIComponent(p)) : '';
}
export function tqLoadTab(name) {
  if (name === 'deps') return loadDeps();
  else if (name === 'tasks') return loadTaskStats();
  else if (name === 'timeline') return loadTimeline();
  else if (name === 'schedule') return loadSchedules();
  else if (name === 'alerts') return loadAlerts();
  else if (name === 'audit') return loadAudit();
  else if (name === 'api') return loadApiTokens();
  else if (name === 'docs') return loadDocsPage();
}

// Single-flight loader: never two requests in flight, and the next request is
// scheduled `gap` ms after the previous one *finished* (success or failure).
// A failed/timed-out run (fn resolves false or throws) doubles the gap up to 120 s.
export function tqLoop(name, fn, opts) {
  // Lazy state: the first call can happen during script load, before later consts initialise.
  const _tqLoops = (window.__tqLoops = window.__tqLoops || {});
  opts = opts || {};
  const base = opts.minGapMs || 5000;
  const st = _tqLoops[name] || (_tqLoops[name] = { inFlight: false, again: false, lastDone: 0, timer: null, gap: base, base });
  st.base = base;
  if (opts.force) {
    if (st.timer) { clearTimeout(st.timer); st.timer = null; }
  } else if (document.hidden) {
    return Promise.resolve();
  }
  // Hand back the running load so callers (tqBusy) wait for the data, not for nothing.
  if (st.inFlight) { st.again = true; return st.promise || Promise.resolve(); }
  const wait = opts.force ? 0 : Math.max(0, st.lastDone + st.gap - Date.now());
  if (wait > 0) {
    if (!st.timer) st.timer = setTimeout(() => { st.timer = null; tqLoop(name, fn, { minGapMs: st.base }); }, wait);
    return Promise.resolve();
  }
  st.inFlight = true;
  const done = ok => { st.gap = ok === false ? Math.min(120000, st.gap * 2) : st.base; };
  st.promise = Promise.resolve().then(fn).then(done, () => done(false)).finally(() => {
    st.inFlight = false;
    st.promise = null;
    st.lastDone = Date.now();
    if (st.again) { st.again = false; tqLoop(name, fn, { minGapMs: st.base }); }
  });
  return st.promise;
}

// Named targets for data-<event> delegation (a call is `[name, ...args]`, so compound inline
// statements live here instead of in markup).
export function tqSetLang(sel) { localStorage.setItem('tq_lang', sel.value); window.location.reload(); }
export function tqRefreshBusy() { tqBusy(refresh()); }
export function tqTimelineBusy() { tqBusy(loadTimeline()); }
export function tqMobSearchInput() { renderMobTaskList().catch(() => {}); }
export function tqSelectVisibleFrom(el) { tqToggleSelectVisible(el.checked); }

/**
 * Loading indicator for loads the operator started (tab switch, Refresh, filter change):
 * a thin animated bar on top once the load takes >300 ms, plus a spinner inside the page
 * until that page's first load finishes. Background polls do not call this, so they stay quiet.
 */
export function tqBusy(work, pageId) {
  // Lazy state like tqLoop: the boot tab load runs before later `let`s initialise (TDZ).
  const st = (window.__tqBusy = window.__tqBusy || { count: 0, timer: null, loaded: {} });
  const p = Promise.resolve(work);
  st.count++;
  if (!st.timer) {
    st.timer = setTimeout(() => {
      st.timer = null;
      if (st.count > 0) document.body.classList.add('tq-busy');
    }, 300);
  }
  const page = pageId && !st.loaded[pageId] ? document.getElementById(pageId) : null;
  let spinner = null;
  if (page && !page.querySelector(':scope > .tq-page-spinner')) {
    spinner = document.createElement('div');
    spinner.className = 'tq-page-spinner';
    spinner.setAttribute('role', 'status');
    // data-i18n: at boot the catalog may not be loaded yet; applyDOM() swaps the text in later.
    const label = i18n.t('status.loading');
    spinner.innerHTML = '<span class="tq-spin" aria-hidden="true"></span><span data-i18n="status.loading">'
      + escHtml(label === 'status.loading' ? 'Loading…' : label) + '</span>';
    page.prepend(spinner);
  }
  const end = () => {
    st.count = Math.max(0, st.count - 1);
    if (!st.count) {
      document.body.classList.remove('tq-busy');
      if (st.timer) { clearTimeout(st.timer); st.timer = null; }
    }
    if (pageId) st.loaded[pageId] = true;
    if (spinner) spinner.remove();
  };
  p.then(end, end);
  return p;
}
export function tqRefreshActivePage() {
  const active = document.querySelector('.page.active');
  if (!active) return;
  // Docs are static until the operator picks another guide — reloading wipes scroll.
  if (active.id === 'docs') return;
  if (active.id === 'timeline') { loadTimeline(true); return; }
  tqLoop('tab:' + active.id, () => tqLoadTab(active.id), { minGapMs: 5000 });
}
export function _showPageEmpty(emptyId, wrapId, showEmpty) {
  const empty = document.getElementById(emptyId);
  const wrap = document.getElementById(wrapId);
  if (empty) empty.hidden = !showEmpty;
  if (wrap) wrap.style.display = showEmpty ? 'none' : '';
}
