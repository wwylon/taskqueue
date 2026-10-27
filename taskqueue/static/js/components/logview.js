// Log panel helpers that do not depend on app state.
import { escHtml } from '../util/format.js';

// Browsers allow ~6 connections per origin; unbounded log streams starve fetch().
export const LOG_SSE_MAX = 3;

/** One log line as HTML: ANSI colour codes stripped, level prefix mapped to a CSS class. */
export function logLineHtml(line) {
  const plain = String(line).replace(/\x1b\[[0-9;]*m/g, '');
  const m = plain.match(/^\[(INFO|DEBUG|PROGRESS|WARN|ERROR)\]/);
  const cls = m ? (' log-' + m[1].toLowerCase()) : '';
  return `<div class="tq-log-line${cls}">${escHtml(plain)}</div>`;
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
  const DIM_CODES = new Set(['2','2;'+'37']);
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
