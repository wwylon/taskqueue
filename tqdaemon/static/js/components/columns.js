// Queue column chooser: hide optional columns of the Queue table. The choice is per browser
// (localStorage) and applied as one generated <style>, so re-renders need no extra work.
// Uses app.js globals at call time: i18n, tqAct, TQ_THIS.
import { escHtml } from '../util/format.js';

// nth-child positions in the Queue table (1 = select box, 3 = Project/Task, 9 = Actions stay visible).
export const QUEUE_OPTIONAL_COLS = [
  { n: 2, key: 'priority', label: 'queue.thPriority' },
  { n: 4, key: 'status', label: 'queue.thStatus' },
  { n: 5, key: 'started', label: 'queue.thStarted' },
  { n: 6, key: 'finished', label: 'queue.thFinished' },
  { n: 7, key: 'resources', label: 'queue.thResources' },
  { n: 8, key: 'description', label: 'queue.thDescription' },
];
const _KEY = 'tq_queue_hidden_cols';

export function tqHiddenQueueCols() {
  try {
    const v = JSON.parse(localStorage.getItem(_KEY) || '[]');
    return Array.isArray(v) ? v.filter(k => QUEUE_OPTIONAL_COLS.some(c => c.key === k)) : [];
  } catch (_) { return []; }
}

/** CSS hiding the given columns in the header and in task rows (log/history sub-rows keep their colspan cell). */
export function tqQueueColumnsCss(hidden) {
  return QUEUE_OPTIONAL_COLS.filter(c => hidden.includes(c.key))
    .map(c => `#queue thead th:nth-child(${c.n}), #queue-tbody tr[id^="qrow-"] > td:nth-child(${c.n}) { display: none; }`)
    .join('\n');
}

export function tqApplyQueueColumns() {
  let el = document.getElementById('tq-colhide');
  if (!el) {
    el = document.createElement('style');
    el.id = 'tq-colhide';
    document.head.appendChild(el);
  }
  el.textContent = tqQueueColumnsCss(tqHiddenQueueCols());
}

export function tqSetQueueColumn(key, visible) {
  const hidden = new Set(tqHiddenQueueCols());
  if (visible) hidden.delete(key); else hidden.add(key);
  try { localStorage.setItem(_KEY, JSON.stringify([...hidden])); } catch (_) {}
  tqApplyQueueColumns();
}

export function tqToggleColumnsMenu() {
  const pop = document.getElementById('queue-cols-pop');
  if (!pop) return;
  if (!pop.hidden) { pop.hidden = true; return; }
  const hidden = tqHiddenQueueCols();
  pop.innerHTML = QUEUE_OPTIONAL_COLS.map(c => `<label class="tq-col-opt"><input type="checkbox" ${hidden.includes(c.key) ? '' : 'checked'}
    data-change="${tqAct([['tqSetQueueColumnFromBox', c.key, TQ_THIS]])}"> ${escHtml(i18n.t(c.label))}</label>`).join('');
  pop.hidden = false;
}

export function tqSetQueueColumnFromBox(key, box) {
  tqSetQueueColumn(key, !!(box && box.checked));
}

if (typeof document !== 'undefined' && document.addEventListener) {
  document.addEventListener('click', e => {
    const pop = document.getElementById('queue-cols-pop');
    const btn = document.getElementById('queue-cols-btn');
    if (!pop || pop.hidden) return;
    if (pop.contains(e.target) || (btn && btn.contains(e.target))) return;
    pop.hidden = true;
  });
}
