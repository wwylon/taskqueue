// Log panel helpers that do not depend on app state.
/* eslint no-control-regex: "off" -- ANSI escape sequences ([...m) are the whole point of these regexes */
import { escHtml } from '../util/format.js';

// Browsers allow ~6 connections per origin; unbounded log streams starve fetch().
export const LOG_SSE_MAX = 3;

/** One log line as HTML: ANSI colour codes stripped, level prefix mapped to a CSS class. */
export function logLineHtml(line) {
  const raw = String(line);
  const plain = raw.replace(/\x1b\[[0-9;]*[A-Za-z]/g, '');
  const kind = classifyLogLine(plain);
  const m = plain.match(/^\[(INFO|DEBUG|PROGRESS)\]/);
  const cls = kind === 'errors' ? ' log-error' : kind === 'warnings' ? ' log-warn' : m ? (' log-' + m[1].toLowerCase()) : '';
  // Keep the task's own ANSI colours (SGR only); ansiToHtml escapes first, so the line stays inert.
  const body = /\x1b\[[0-9;]*m/.test(raw) ? ansiToHtml(raw.replace(/\x1b\[[0-9;]*[A-Za-ln-z]/g, '')) : escHtml(plain);
  return `<div class="tq-log-line${cls}">${body}</div>`;
}

// Mirror of tqdaemon.log_issues.classify_log_line (the run's warn/error chip counts), so the dock's level filter
// and "first error" agree with the counts. Error tokens are case-sensitive: "0 errors" / "an error" are prose.
const _ERR_RE = /\b(?:ERROR|FATAL|CRITICAL|REJECT)\b/;
const _ERR_TITLE_RE = /\b(?:Error|Fatal|Critical|Reject):/;
const _WARN_RE = /\bWARN(?:ING)?\b/i;
const _OTHER_RE = /LOW SCORE/i;

/** 'errors' | 'warnings' | 'other' | null for one log line (ANSI already stripped or not). */
export function classifyLogLine(line) {
  const text = String(line || '').replace(/\x1b\[[0-9;]*[A-Za-z]/g, '').trim();
  if (!text) return null;
  if (_ERR_RE.test(text) || _ERR_TITLE_RE.test(text)) return 'errors';
  if (_WARN_RE.test(text)) return 'warnings';
  if (_OTHER_RE.test(text)) return 'other';
  return null;
}

/**
 * Register a log EventSource in `map` (keyed by runId): close any stream already held for this run and
 * evict the oldest streams across all `maps` past LOG_SSE_MAX (never the one just added).
 */
export function trackLogSse(maps, map, runId, es) {
  if (map[runId]) { try { map[runId].close(); } catch (e) {} }
  delete map[runId];
  map[runId] = es;
  const all = [];
  maps.forEach(m => Object.keys(m).forEach(k => all.push([m, k])));
  while (all.length > LOG_SSE_MAX) {
    const [m, k] = all.shift();
    if (m === map && k === String(runId)) continue;
    try { m[k].close(); } catch (e) {}
    delete m[k];
  }
}

/** Full log text as HTML for the expanded log panes: ANSI SGR colours kept as spans, keyword colouring otherwise. */
export function ansiToHtml(raw) {
  // Security: escape HTML first — log lines are untrusted.
  const escaped = escHtml(raw);
  // Then render ANSI SGR sequences as <span> elements.
  const ANSI_COLORS = {
    '30':'#555','31':'#e74c3c','32':'#2ecc71','33':'#f39c12',
    '34':'#5b7cf6','35':'#a855f7','36':'#06b6d4','37':'#ccc',
    '90':'#888','91':'#ff6b6b','92':'#6ef08d','93':'#ffd93d',
    '94':'#74a0fb','95':'#c084fc','96':'#67e8f9','97':'#fff',
  };
  const BOLD_CODES = new Set(['1']);
  let html = '';
  let spanOpen = false;
  const parts = escaped.split(/(\x1b\[[0-9;]*m)/);
  for (const part of parts) {
    const m = part.match(/^\x1b\[([0-9;]*)m$/);
    if (m) {
      if (spanOpen) { html += '</span>'; spanOpen = false; }
      const codes = m[1].split(';');
      const fg = codes.find(c => ANSI_COLORS[c]);
      const bold = codes.some(c => BOLD_CODES.has(c));
      const dim = codes.some(c => c === '2');
      if (codes[0] === '0' || codes[0] === '') continue;
      const style = [
        fg ? `color:${ANSI_COLORS[fg]}` : '',
        bold ? 'font-weight:bold' : '',
        dim ? 'opacity:0.6' : '',
      ].filter(Boolean).join(';');
      if (style) { html += `<span style="${style}">`; spanOpen = true; }
    } else {
      // Keyword colouring fallback for lines without ANSI codes — per line, so one "ERROR" does not tint the chunk.
      if (!spanOpen) {
        html += part.split('\n').map(line => {
          const up = line.toUpperCase();
          if (up.includes('ERROR') || up.includes('FATAL') || up.includes('CRITICAL')) return `<span style="color:#e74c3c">${line}</span>`;
          if (up.includes('WARN')) return `<span style="color:#f39c12">${line}</span>`;
          if (up.includes('DEBUG') || up.includes('TRACE')) return `<span style="opacity:0.6">${line}</span>`;
          return line;
        }).join('\n');
        continue;
      }
      html += part;
    }
  }
  if (spanOpen) html += '</span>';
  return html;
}
