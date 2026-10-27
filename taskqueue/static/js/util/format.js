// Pure formatting / escaping helpers (no dependency on app state).

// Same breakpoint as app.css: below it Queue/History show cards, above it tables.
export const MOBILE_LAYOUT_QUERY = '(max-width: 768px)';
export function isMobileLayout() {
  try { return !!(typeof window !== 'undefined' && window.matchMedia && window.matchMedia(MOBILE_LAYOUT_QUERY).matches); }
  catch (_) { return false; }
}
// Re-render when the layout flips (rotate, resize across the breakpoint): only the visible one is built.
export function onLayoutChange(fn) {
  try {
    const mq = typeof window !== 'undefined' && window.matchMedia ? window.matchMedia(MOBILE_LAYOUT_QUERY) : null;
    if (mq && mq.addEventListener) mq.addEventListener('change', fn);
  } catch (_) {}
}

export function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

// JS string literal safe inside a double-quoted HTML attribute (inline handlers).
export function jsq(s) { return escHtml(JSON.stringify(String(s))); }

export function taskKey(project, taskId) { return `${project}/${taskId}`; }

// Queue-row DOM id; the project length keeps `a-b`+`c` distinct from `a`+`b-c`.
export function qrowId(project, taskId) { return `qrow-${String(project).length}-${project}-${taskId}`; }

/** Safe fragment for HTML element ids (F-111). Use on both id= and getElementById — never escHtml. */
export function domIdPart(s) {
  return String(s).replace(/[^A-Za-z0-9_-]/g, (c) => '_' + c.charCodeAt(0).toString(16));
}

export function fmt(s) {
  if (!s) return '—';
  const d = new Date(s);
  return isNaN(d) ? escHtml(s) : d.toLocaleString();
}

export function dur(sec) {
  if (sec == null || sec < 0) return '—';
  const h = Math.floor(sec/3600), m = Math.floor((sec%3600)/60), s = Math.floor(sec%60);
  if (h) return `${h}h${String(m).padStart(2,'0')}m`;
  if (m) return `${m}m${String(s).padStart(2,'0')}s`;
  return `${s}s`;
}

export const LOG_CAP_CHARS = 256 * 1024;

// Append a streamed line, keeping only the tail so a long-running log cannot grow without bound.
export function appendLog(state, line) {
  let c = state.content + line + '\n';
  if (c.length > LOG_CAP_CHARS) {
    c = c.slice(c.length - LOG_CAP_CHARS);
    const nl = c.indexOf('\n');
    if (nl >= 0) c = c.slice(nl + 1);
  }
  state.content = c;
}

/**
 * Stalled-vs-slow chip for a running task from `progress_health` ({age_sec, stalled, eta_sec}):
 * a warning when the last % change is far older than usual, else the age once it passes a minute.
 */
export function progressHealthHtml(ph) {
  if (!ph || ph.age_sec == null) return '';
  const t = (k, v) => (globalThis.i18n ? globalThis.i18n.t(k, v) : k);
  const ago = dur(ph.age_sec);
  if (ph.stalled) {
    return `<span class="tq-stall" title="${escHtml(t('progress.stalledTitle'))}">⚠ ${escHtml(t('progress.stalled', { ago }))}</span>`;
  }
  return ph.age_sec >= 60 ? `<span class="tq-progress-age">${escHtml(t('progress.lastChange', { ago }))}</span>` : '';
}
