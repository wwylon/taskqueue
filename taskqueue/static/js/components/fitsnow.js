// Dashboard "What fits now" strip: free CPU / RAM / VRAM after running tasks, and the queued task
// the scheduler would start next (or why the head of the queue is blocked). Data: GET /api/plan
// (a read-only dry run; approximate, see docs/scheduler.md). Uses the i18n global at call time.
import { S } from '../core/state.js';
import { escHtml } from '../util/format.js';

export const FITS_NOW_MIN_INTERVAL_MS = 15000;

const _n = v => (Math.round(Number(v || 0) * 10) / 10).toString();

/** Strip HTML for a /api/plan payload (pure; exported for tests). */
export function _fitsNowHtml(plan) {
  if (!plan || !plan.headroom) return '';
  const h = plan.headroom;
  const free = [
    `CPU ${_n(h.cpu_cores)} cores`,
    `RAM ${_n(h.ram_gib)} GiB`,
    ...(Number(h.gpu_vram_gib || 0) > 0 ? [`VRAM ${_n(h.gpu_vram_gib)} GiB`] : []),
  ].join(' · ');
  const tasks = plan.tasks || [];
  const next = tasks.find(t => t.verdict === 'start');
  let tail;
  if (next) {
    tail = i18n.t('dashboard.fitsNext', {task: `${next.project}/${next.task_id}`, n: plan.would_start || 1});
  } else if (tasks.length) {
    const head = tasks[0];
    tail = i18n.t('dashboard.fitsBlocked', {task: `${head.project}/${head.task_id}`, reason: head.reason || head.verdict});
  } else {
    tail = i18n.t('dashboard.fitsNothingQueued');
  }
  return `<span class="fits-now-label">${escHtml(i18n.t('dashboard.fitsNow'))}</span> `
    + `<span class="fits-now-free">${escHtml(free)}</span>`
    + `<span class="fits-now-next">${escHtml(tail)}</span>`;
}

let _inflight = null;

/** Refresh the strip when the dashboard is visible; at most every FITS_NOW_MIN_INTERVAL_MS unless forced. */
export async function tqRefreshFitsNow(force = false) {
  const el = document.getElementById('fits-now');
  if (!el || document.hidden) return;
  const page = document.getElementById('dashboard');
  if (page && !page.classList.contains('active')) return;
  const now = Date.now();
  if (_inflight || (!force && S.fitsNowAt && now - S.fitsNowAt < FITS_NOW_MIN_INTERVAL_MS)) return;
  S.fitsNowAt = now;
  _inflight = (async () => {
    try {
      const r = await fetch('/api/plan?limit=20');
      if (!r.ok) return;
      const html = _fitsNowHtml(await r.json());
      el.innerHTML = html;
      el.hidden = !html;
    } catch (_) {
      // A failed dry run just leaves the last strip; the gauges above stay live.
    } finally {
      _inflight = null;
    }
  })();
  await _inflight;
}
