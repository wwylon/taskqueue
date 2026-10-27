// Task stats table (Tasks tab): fetch, client-side search with debounce, and render. Rows live in S.taskStatsRows.
// Uses app.js globals at call time (tqProjectQuery, _fetchJson, tqAct, i18n).
import { S } from '../core/state.js';
import { projStyle } from '../util/projcolor.js';


export function onTasksSearchInput() {
  _syncTasksSearchClear();
  const inp = document.getElementById('tasks-search');
  if (inp) inp.classList.add('tq-searching');
  if (S.tasksSearchDebounce) clearTimeout(S.tasksSearchDebounce);
  S.tasksSearchDebounce = setTimeout(() => {
    S.tasksSearchDebounce = null;
    if (inp) inp.classList.remove('tq-searching');
    _renderTaskStatsTable();
  }, 250);
}
export function _syncTasksSearchClear() {
  const inp = document.getElementById('tasks-search');
  const btn = document.getElementById('tasks-search-clear');
  if (!btn) return;
  btn.hidden = !(inp && inp.value);
}
export function clearTasksSearch() {
  const inp = document.getElementById('tasks-search');
  if (inp) {
    inp.value = '';
    inp.classList.remove('tq-searching');
  }
  _syncTasksSearchClear();
  _renderTaskStatsTable();
}
export function _renderTaskStatsTable() {
  const tb = document.getElementById('tasks-tbody');
  if (!tb) return;
  const q = ((document.getElementById('tasks-search') || {}).value || '').trim().toLowerCase();
  let rows = S.taskStatsRows || [];
  if (q) {
    rows = rows.filter(r =>
      String(r.project || '').toLowerCase().includes(q)
      || String(r.task_id || '').toLowerCase().includes(q)
    );
  }
  const emptyAll = (S.taskStatsRows || []).length === 0;
  const emptyFilter = !emptyAll && rows.length === 0;
  const emptyEl = document.getElementById('tasks-empty');
  const filterEl = document.getElementById('tasks-filter-empty');
  const wrap = document.getElementById('tasks-wrap');
  if (emptyEl) emptyEl.hidden = !emptyAll;
  if (filterEl) filterEl.hidden = !emptyFilter;
  if (wrap) wrap.style.display = (emptyAll || emptyFilter) ? 'none' : '';
  tb.innerHTML = rows.map(r => `<tr>
    <td><span class="proj-tag" style="${projStyle(r.project)}">${escHtml(r.project)}</span></td>
    <td class="task-id-mono">${escHtml(r.task_id)}</td>
    <td>${escHtml(fmt(r.last_finish))}</td>
    <td>${escHtml(dur(r.mean_sec))}</td>
    <td>${escHtml(dur(r.p50_sec))}</td>
    <td>${escHtml(dur(r.p95_sec))}</td>
    <td>${escHtml(String(r.count))}</td>
    <td>${escHtml(Math.round((r.success_rate || 0) * 100) + '%')}</td>
    <td><button class="cfg-btn" data-click="${tqAct([['tqOpenDrawer', String(r.project), String(r.task_id)]])}">${escHtml(i18n.t('btn.detail') || 'Detail')}</button></td>
  </tr>`).join('');
}
export async function loadTaskStats() {
  const data = await _fetchJson('/api/task-stats' + tqProjectQuery(), 8000, true);
  if (!data) return false;  // keep last good rows on transient failure
  S.taskStatsRows = data.tasks || [];
  _syncTasksSearchClear();
  _renderTaskStatsTable();
}
