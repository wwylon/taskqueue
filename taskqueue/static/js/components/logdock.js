// Bottom log dock (#tq-log-panel): static one-shot load or live SSE tail of a run's log, capped at 200 lines.
// The open EventSource is module-local; the log-line renderer comes from logview.js.
import { logLineHtml as tqLogLineHtml } from './logview.js';
import { tqEventSource, latest } from '../core/api.js';

let _logPanelEs = null;
let _dockRun = null;       // { runId, project, taskId } of the open dock
let _filterRe = null;      // RegExp from the filter box, or null

/** Filter text -> RegExp (case-insensitive). Invalid regex falls back to a literal match. */
export function tqLogFilterRegex(text) {
  const q = String(text || '').trim();
  if (!q) return null;
  try { return new RegExp(q, 'i'); } catch (_) { return new RegExp(q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'i'); }
}
function _applyFilterTo(el) {
  if (el && el.classList && el.classList.contains('tq-log-line')) el.hidden = !!_filterRe && !_filterRe.test(el.textContent || '');
}
export function tqSetLogFilter(input) {
  _filterRe = tqLogFilterRegex(input && input.value);
  const body = document.getElementById('tq-log-body');
  if (body) Array.from(body.children).forEach(_applyFilterTo);
}
export function tqSetLogLevel(sel) {
  const body = document.getElementById('tq-log-body');
  if (!body) return;
  body.classList.toggle('lvl-warn', sel && sel.value === 'warn');
  body.classList.toggle('lvl-error', sel && sel.value === 'error');
}
export function tqToggleLogWrap() {
  const body = document.getElementById('tq-log-body');
  if (body) body.classList.toggle('nowrap');
}
/** Save the run's log (server tail up to 8 MiB) as <project>_<task>_<run>.log. */
export async function tqDownloadLog() {
  if (!_dockRun) return;
  const r = await fetch(`/api/logs/${_dockRun.runId}?bytes=${8 * 1024 * 1024}`);
  if (!r.ok) return;
  const blob = new Blob([await r.text()], { type: 'text/plain' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `${_dockRun.project}_${_dockRun.taskId}_${_dockRun.runId}.log`.replace(/[^\w.-]+/g, '_');
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

// Live log messages can arrive many times a second: queue them and write once per frame.
let _logQueue = '';
let _logRaf = 0;
export function tqAppendLogText(text) {
  if (typeof requestAnimationFrame !== 'function') { _writeLogText(String(text)); return; }
  _logQueue += String(text);
  if (_logRaf) return;
  _logRaf = requestAnimationFrame(() => {
    _logRaf = 0;
    const t = _logQueue;
    _logQueue = '';
    _writeLogText(t);
  });
}
function _writeLogText(text) {
  const body = document.getElementById('tq-log-body');
  if (!body) return;
  const lines = String(text).split('\n');
  if (lines.length && lines[lines.length - 1] === '') lines.pop();
  const html = lines.map(tqLogLineHtml).join('');
  const before = body.children.length;
  if (html) body.insertAdjacentHTML('beforeend', html);
  if (_filterRe) for (let i = before; i < body.children.length; i++) _applyFilterTo(body.children[i]);
  while (body.children.length > 200) body.removeChild(body.firstChild);
  body.scrollTop = body.scrollHeight;
}
export function tqCloseLog() {
  if (_logPanelEs) { _logPanelEs.close(); _logPanelEs = null; }
  const panel = document.getElementById('tq-log-panel');
  if (panel) panel.hidden = true;
  document.body.classList.remove('tq-log-open');
  const live = document.getElementById('tq-log-live');
  if (live) live.hidden = true;
}
export function tqClearLogView() {
  _logQueue = '';
  const body = document.getElementById('tq-log-body');
  if (body) body.innerHTML = '';
}
export function tqOpenLogPanel(runId, project, taskId, isRunning) {
  if (_logPanelEs) { _logPanelEs.close(); _logPanelEs = null; }
  const panel = document.getElementById('tq-log-panel');
  const title = document.getElementById('tq-log-title');
  const body = document.getElementById('tq-log-body');
  const live = document.getElementById('tq-log-live');
  if (!panel || !body) return;
  panel.hidden = false;
  document.body.classList.add('tq-log-open');
  if (title) title.textContent = (project || '') + '/' + (taskId || '') + ' #' + runId;
  _dockRun = { runId, project: project || '', taskId: taskId || '' };
  if (live) live.hidden = !isRunning;
  body.innerHTML = '';
  if (isRunning && typeof EventSource !== 'undefined') {
    const es = tqEventSource(`/api/logs/${runId}/stream`);
    _logPanelEs = es;
    es.onmessage = ev => {
      if (_logPanelEs !== es) return;
      tqAppendLogText(ev.data + '\n');
    };
    es.onerror = () => {
      if (_logPanelEs !== es) return;
      const empty = !body.textContent.trim();
      es.close();
      if (_logPanelEs === es) _logPanelEs = null;
      if (live) live.hidden = true;
      if (empty) tqLoadStaticLog(runId);
    };
  } else {
    tqLoadStaticLog(runId);
  }
}
export async function tqLoadStaticLog(runId) {
  const body = document.getElementById('tq-log-body');
  if (!body) return;
  const live = latest('static-log');
  try {
    const r = await fetch(`/api/logs/${runId}`);
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const text = await r.text();
    if (!live()) return;
    body.innerHTML = '';
    const lines = text.split('\n');
    tqAppendLogText(lines.slice(-200).join('\n'));
  } catch (e) {
    if (live()) body.innerHTML = tqLogLineHtml(String(e.message || e));
  }
}
