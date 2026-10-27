// Log panel search: highlight/navigate matches inside an already-loaded <pre>.
// Per-panel state lives in S.logSearch (preId -> { query, current }).
import { S } from '../core/state.js';
import { tqEventSource } from '../core/api.js';
import { appendLog } from '../util/format.js';
import { trackLogSse } from './logview.js';

export function applyLogSearch(preId, query, currentIdx) {
  const pre = document.getElementById(preId);
  if (!pre) return;
  pre.querySelectorAll('.log-search-hl').forEach(s => s.replaceWith(document.createTextNode(s.textContent)));
  pre.normalize();

  const countEl = document.getElementById('log-search-count-' + preId);
  if (!query) { if (countEl) countEl.textContent = ''; return; }

  const walk = document.createTreeWalker(pre, NodeFilter.SHOW_TEXT);
  const matches = [];
  let node;
  while ((node = walk.nextNode())) {
    let start = 0;
    const lc = node.textContent.toLowerCase();
    while (true) {
      const idx = lc.indexOf(query, start);
      if (idx < 0) break;
      matches.push({ node, idx });
      start = idx + query.length;
    }
  }
  if (!matches.length) { if (countEl) countEl.textContent = '0 matches'; return; }

  currentIdx = Math.max(0, Math.min(currentIdx, matches.length - 1));
  if (S.logSearch[preId]) S.logSearch[preId].current = currentIdx;

  // Highlight in reverse to preserve offsets
  [...matches].reverse().forEach(({ node, idx }) => {
    const span = document.createElement('span');
    span.className = 'log-search-hl';
    span.style.background = '#f59e0b';
    span.style.color = '#000';
    span.style.borderRadius = '2px';
    node.splitText(idx + query.length);
    const match = node.splitText(idx);
    span.textContent = match.textContent;
    match.replaceWith(span);
  });

  // allHl is DOM (forward) order, so allHl[i] corresponds to matches[i]
  const active = pre.querySelectorAll('.log-search-hl')[currentIdx];
  if (active) {
    active.style.background = '#ef4444';
    active.style.color = '#fff';
    active.scrollIntoView({ block: 'nearest' });
  }
  if (countEl) countEl.textContent = `${currentIdx + 1} / ${matches.length}`;
}

export function searchLog(input, runId, preId) {
  const query = input.value.trim().toLowerCase();
  S.logSearch[preId] = { query, current: 0 };
  applyLogSearch(preId, query, 0);
}

export function logSearchNav(preId, delta) {
  const state = S.logSearch[preId];
  if (!state || !state.query) return;
  const pre = document.getElementById(preId);
  if (!pre) return;
  const spans = pre.querySelectorAll('.log-search-hl');
  if (!spans.length) return;
  spans[state.current].style.background = '#f59e0b';
  spans[state.current].style.color = '#000';
  state.current = (state.current + delta + spans.length) % spans.length;
  const active = spans[state.current];
  active.style.background = '#ef4444';
  active.style.color = '#fff';
  active.scrollIntoView({ block: 'nearest' });
  const countEl = document.getElementById('log-search-count-' + preId);
  if (countEl) countEl.textContent = `${state.current + 1} / ${spans.length}`;
}

// ---- Live log streaming (shared by the queue-row and queue-card log panels) ----

/**
 * Open an SSE tail of a running task's log into <pre id=preId>.
 * `maps` = every SSE registry (for the global cap), `map` = this panel's registry,
 * `stateKey` = S-independent key into the `expanded` store so DOM rebuilds restore history.
 * Rendering helpers (_ansiToHtml, _sseAppend, _fetchLog, i18n) are late-bound globals from app.js.
 */
export function openLogStream({ runId, maps, map, preId, followId, expanded, stateKey }) {
  const pre = document.getElementById(preId);
  if (pre) pre.textContent = '';
  const sse = tqEventSource(`/api/logs/${runId}/stream`);
  trackLogSse(maps, map, runId, sse);
  sse.onmessage = ev => {
    const pre2 = document.getElementById(preId);
    if (!pre2) { sse.close(); delete map[runId]; return; }
    window._sseAppend(pre2, window._ansiToHtml(ev.data + '\n'));
    const state = expanded[stateKey];
    if (state) appendLog(state, ev.data);
    const follow = document.getElementById(followId);
    if (!follow || follow.checked) pre2.scrollTop = pre2.scrollHeight;
  };
  let ended = false;
  let drops = 0;
  if (sse.addEventListener) sse.addEventListener('end', () => { ended = true; });
  sse.onerror = () => {
    const pre2 = document.getElementById(preId);
    const empty = !pre2 || !String(pre2.textContent || '').trim();
    // A dropped connection (not the server's `end`) is retried by the browser, resuming
    // from Last-Event-ID; give up after a few consecutive failures.
    if (!ended && pre2 && sse.readyState === EventSource.CONNECTING && ++drops <= 5) return;
    const closed = ended || sse.readyState === EventSource.CLOSED;
    sse.close(); delete map[runId];
    // Transient stream 404 (stale pool DB snapshot): fall back to a static tail.
    if (empty) { window._fetchLog(runId, preId); return; }
    if (pre2 && closed) {
      const endSpan = document.createElement('span');
      endSpan.style.color = 'var(--muted)';
      endSpan.textContent = window.i18n.t('log.endOfLog');
      pre2.appendChild(endSpan);
    }
  };
  return sse;
}

/** Last `maxLines` lines of a log text (CRLF-normalised, trailing blank lines dropped). */
export function logTailText(text, maxLines) {
  const lines = String(text).replace(/\r\n/g, '\n').replace(/\r/g, '\n').split('\n');
  while (lines.length && lines[lines.length - 1] === '') lines.pop();
  return lines.slice(-maxLines).join('\n');
}

// ---- History log toggles (state in S.expandedLogs; _fetchLog is still an app.js global) ----
export async function toggleHistoryLog(runId, rowId, isRunning) {
  // W3.2: panel-scoped key
  const key = `h:${runId}`;
  const row = document.getElementById(rowId);
  if (!row) return;
  if (S.expandedLogs[key]) {
    delete S.expandedLogs[key];
    row.classList.remove('open');
    const btn = document.getElementById(`h-logbtn-${runId}`);
    if (btn) btn.textContent = i18n.t('btn.log');
    return;
  }
  S.expandedLogs[key] = { content: '', isRunning };
  row.classList.add('open');
  const btn = document.getElementById(`h-logbtn-${runId}`);
  if (btn) btn.textContent = i18n.t('btn.hideLog');
  row.scrollIntoView({ block: 'nearest' });
  await _fetchLog(runId, `h-log-pre-${runId}`);
}

export async function toggleHistoryCardLog(runId, isRunning) {
  const key = `h:${runId}`;
  const wrap = document.getElementById(`hc-log-wrap-${runId}`);
  if (!wrap) return;
  if (S.expandedLogs[key]) {
    delete S.expandedLogs[key];
    wrap.style.display = 'none';
    const btn = document.getElementById(`hc-logbtn-${runId}`);
    if (btn) btn.textContent = i18n.t('btn.log');
    return;
  }
  S.expandedLogs[key] = { content: 'loading…', isRunning };
  wrap.style.display = 'block';
  const btn = document.getElementById(`hc-logbtn-${runId}`);
  if (btn) btn.textContent = i18n.t('btn.hideLog');
  wrap.scrollIntoView({ block: 'nearest' });
  await _fetchLog(runId, `hc-log-pre-${runId}`);
}

export async function tailHistoryCardLog(runId) {
  await _fetchLog(runId, `hc-log-pre-${runId}`);
  const pre = document.getElementById(`hc-log-pre-${runId}`);
  if (pre) pre.scrollTop = pre.scrollHeight;
}

export async function tailHistoryLog(runId) {
  await _fetchLog(runId, `h-log-pre-${runId}`);
  const pre = document.getElementById(`h-log-pre-${runId}`);
  if (pre) pre.scrollTop = pre.scrollHeight;
}


// ---- Queue log toggles ----
export async function toggleQueueLog(runId, rowId, isRunning) {
  // W3.2: panel-scoped key
  const key = `q:${runId}`;
  const row = document.getElementById(rowId);
  if (!row) return;
  if (S.expandedLogs[key]) {
    delete S.expandedLogs[key];
    row.classList.remove('open');
    const btn = document.getElementById(`q-logbtn-${runId}`);
    if (btn) btn.textContent = i18n.t('btn.log');
    // E2: close SSE if open
    if (S.logSseMap[runId]) { S.logSseMap[runId].close(); delete S.logSseMap[runId]; }
    return;
  }
  S.expandedLogs[key] = { content: '', isRunning };
  row.classList.add('open');
  const btn = document.getElementById(`q-logbtn-${runId}`);
  if (btn) btn.textContent = i18n.t('btn.hideLog');
  row.scrollIntoView({ block: 'nearest' });
  if (isRunning && typeof EventSource !== 'undefined') {
    openLogStream({ runId, maps: [S.logSseMap, S.cardLogSseMap], map: S.logSseMap, preId: `q-log-pre-${runId}`,
      followId: `q-log-follow-${runId}`, expanded: S.expandedLogs, stateKey: key });
  } else {
    await _fetchLog(runId, `q-log-pre-${runId}`);
  }
  _drawSparkline(runId);
}

export async function tailQueueLog(runId) {
  await _fetchLog(runId, `q-log-pre-${runId}`);
  const pre = document.getElementById(`q-log-pre-${runId}`);
  if (pre) pre.scrollTop = pre.scrollHeight;
}


// ---- Dashboard log toggles ----
export async function toggleDashLog(runId, project, taskId) {
  // W3.2: panel-scoped key prevents cross-panel log bleed
  const key = `d:${runId}`;
  const wrap = document.getElementById(`d-log-wrap-${runId}`);
  const btn  = document.getElementById(`d-logbtn-${runId}`);
  if (!wrap) return;
  if (S.expandedLogs[key]) {
    delete S.expandedLogs[key];
    wrap.classList.remove('open');
    if (btn) btn.textContent = i18n.t('btn.log');
    return;
  }
  S.expandedLogs[key] = { content: '', isRunning: true };
  wrap.classList.add('open');
  if (btn) btn.textContent = i18n.t('btn.hideLog');
  wrap.scrollIntoView({ block: 'nearest' });
  await _fetchLog(runId, `d-log-pre-${runId}`);
}

export async function refreshDashLog(runId) {
  await _fetchLog(runId, `d-log-pre-${runId}`);
}

export async function tailDashLog(runId) {
  await _fetchLog(runId, `d-log-pre-${runId}`);
  const pre = document.getElementById(`d-log-pre-${runId}`);
  if (pre) pre.scrollTop = pre.scrollHeight;
}
