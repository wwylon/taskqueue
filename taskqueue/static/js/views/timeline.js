// Timeline tab: per-project Gantt of running/queued/cron bars as inline SVG. Uses app.js globals at call time: tqAct,
// TQ_EVENT, i18n, tqLoop, tqProjectQuery, _fetchJson, _showPageEmpty. The last good payload is module-local so a failed
// poll keeps the previous chart (marked stale).
import { escHtml, dur } from '../util/format.js';
import { projColor } from '../util/projcolor.js';

const _TL_COLOR = {
  running: '#9184d9', completed: '#34d399', failed: '#f87171', queued: '#60a5fa',
  interrupted: '#94a3b8', stopped: '#94a3b8', suspended: '#fbbf24', unknown: '#94a3b8',
  cron: '#34d399',
};
const _TL_TIMEOUT_MS = 30000;
let _tlLast = null;       // last good payload (kept on failure)
let _tlStale = false;
export function _tlClock(d) {
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}
export function _tlDay(d) {
  return d.toLocaleDateString([], { weekday: 'short', day: 'numeric' });
}
export function _tlAt(iso, ref) {
  // HH:MM, prefixed by the weekday/day when it differs from the reference day.
  const d = new Date(iso);
  if (isNaN(d)) return '';
  const r = ref ? new Date(ref) : new Date();
  const same = d.toDateString() === r.toDateString();
  return same ? _tlClock(d) : _tlDay(d) + ' ' + _tlClock(d);
}
export function _tlTicks(startMs, spanSec) {
  const h = spanSec / 3600;
  const stepMin = h <= 2 ? 15 : h <= 6 ? 30 : h <= 24 ? 120 : h <= 72 ? 360 : 1440;
  const stepMs = stepMin * 60000;
  const d0 = new Date(startMs);
  const mid = new Date(d0.getFullYear(), d0.getMonth(), d0.getDate()).getTime();
  const out = [];
  for (let t = mid + Math.ceil((startMs - mid) / stepMs) * stepMs; t <= startMs + spanSec * 1000; t += stepMs) out.push(t);
  return out;
}
export function loadTimeline(auto) {
  // auto=true comes from the periodic dashboard refresh and is rate-limited;
  // everything else (tab switch, Refresh, hours/project change, post-action) is immediate.
  return tqLoop('timeline', _loadTimelineNow, { minGapMs: 15000, force: !auto });
}
export async function _loadTimelineNow() {
  const hours = (document.getElementById('tl-hours') || {}).value || '1';
  const q = tqProjectQuery();
  const url = '/api/timeline' + (q ? q + '&' : '?') + 'hours=' + encodeURIComponent(hours);
  const data = await _fetchJson(url, _TL_TIMEOUT_MS, false);
  const ok = !!(data && Array.isArray(data.bars));
  _tlStale = !ok;
  if (ok) _tlLast = data;
  _renderTimeline(ok ? data : _tlLast, hours);
  return ok;
}
export function _renderTimeline(data, hours) {
  const host = document.getElementById('tl-scroll');
  const bunchEl = document.getElementById('tl-bunch');
  const staleEl = document.getElementById('tl-stale');
  if (staleEl) {
    staleEl.hidden = !_tlStale;
    staleEl.textContent = i18n.t('tl.stale') || 'stale — retrying';
  }
  if (!host || !data) return;
  const bars = data.bars || [];
  _showPageEmpty('tl-empty', 'tl-scroll', bars.length === 0);
  if (bunchEl) {
    if (data.bunch_end_at && bars.length) {
      const tpl = i18n.t('tl.drainedAt') || 'Queue drained ~{time}';
      let txt = tpl.replace('{time}', _tlAt(data.bunch_end_at, data.window_start));
      if (data.truncated) txt += ' · ' + (data.total_bars + ' ' + (i18n.t('tl.tasks') || 'tasks'));
      bunchEl.textContent = txt;
      bunchEl.hidden = false;
    } else {
      bunchEl.hidden = true;
      bunchEl.textContent = '';
    }
  }
  if (!bars.length) { host.innerHTML = ''; return; }
  const span = Number(data.span_sec) || (Number(hours) * 3600);
  const startMs = new Date(data.window_start).getTime();
  const labelW = 300;
  const plotW = Math.max(720, Math.min(1400, Math.round(span / 60)));
  const endPad = 70;
  const hdrH = 22;
  const rowH = 22;
  const h = hdrH + bars.length * rowH + 8;
  const ticks = _tlTicks(startMs, span);
  let svg = `<svg class="tl-svg" width="${labelW + plotW + endPad}" height="${h}" role="img">`;
  ticks.forEach(t => {
    const x = labelW + ((t - startMs) / 1000 / span) * plotW;
    const d = new Date(t);
    const midnight = d.getHours() === 0 && d.getMinutes() === 0;
    const lbl = midnight ? _tlDay(d) : _tlClock(d);
    svg += `<line class="tl-grid" x1="${x.toFixed(1)}" y1="${hdrH - 2}" x2="${x.toFixed(1)}" y2="${h - 4}"/>`;
    if (x - labelW > 36) svg += `<text class="tl-axis" x="${x.toFixed(1)}" y="14" text-anchor="middle">${escHtml(lbl)}</text>`;
  });
  svg += `<text class="tl-axis tl-now" x="${labelW}" y="14" text-anchor="start">${escHtml((i18n.t('tl.axisNow') || 'now') + ' ' + _tlClock(new Date(startMs)))}</text>`;
  svg += `<line class="tl-grid tl-nowline" x1="${labelW}" y1="${hdrH - 2}" x2="${labelW}" y2="${h - 4}"/>`;
  svg += `<line class="tl-grid" x1="${labelW}" y1="${hdrH - 2}" x2="${labelW + plotW}" y2="${hdrH - 2}"/>`;
  bars.forEach((b, i) => {
    const y = hdrH + i * rowH;
    const x = labelW + (Number(b.start_sec) / span) * plotW;
    const w = Math.max(4, ((Number(b.end_sec) - Number(b.start_sec)) / span) * plotW);
    const fill = b.kind === 'cron' ? _TL_COLOR.cron
      : (_TL_COLOR[b.status] || _TL_COLOR[b.kind] || '#94a3b8');
    const label = (b.project ? b.project + '/' : '') + b.task_id;
    const labelTrunc = label.length > 26 ? label.slice(0, 25) + '…' : label;
    const est = b.estimate_source && b.estimate_source !== 'history' ? '~' : '';
    const range = (b.start_at && b.end_at)
      ? _tlAt(b.start_at, data.window_start) + '–' + est + _tlAt(b.end_at, b.start_at) : '';
    const tip = [
      label,
      b.kind || '',
      b.status || '',
      range,
      'est ' + dur(b.duration_sec) + ' (' + (b.estimate_source || '?') + ')',
      b.priority != null ? 'prio ' + b.priority : '',
      b.clipped ? 'clipped' : '',
    ].filter(Boolean).join(' · ');
    const can = b.project && b.task_id;
    const attrs = can
      ? ` data-click="${tqAct([['tqOpenDrawer', String(b.project), String(b.task_id)]])}" data-contextmenu="${tqAct([['showDagCtxMenu', TQ_EVENT, String(b.project), String(b.task_id), b.status || '', {timeline: true, priority: Number(b.priority) || 0}]])}" style="cursor:pointer"`
      : '';
    svg += `<rect x="0" y="${y}" width="${labelW + plotW + endPad}" height="${rowH}" fill="transparent"${attrs}/>`;
    svg += `<text x="4" y="${y + 14}" class="tl-label" style="fill:${projColor(b.project)}"${attrs}>${escHtml(labelTrunc)}</text>`;
    if (range) svg += `<text x="${labelW - 6}" y="${y + 14}" class="tl-axis" text-anchor="end"${attrs}>${escHtml(range)}</text>`;
    svg += `<rect x="${x.toFixed(1)}" y="${y + 4}" width="${w.toFixed(1)}" height="14" rx="3" fill="${fill}" stroke="${projColor(b.project)}" stroke-width="2" opacity="${b.kind === 'running' ? '0.9' : '0.7'}"${attrs}><title>${escHtml(tip)}</title></rect>`;
    if (b.end_at) {
      const ex = x + w + 4;
      svg += `<text class="tl-axis" x="${ex.toFixed(1)}" y="${y + 15}"${attrs}>${escHtml('→ ' + est + _tlAt(b.end_at, b.start_at))}</text>`;
    }
  });
  svg += '</svg>';
  host.innerHTML = svg;
}
