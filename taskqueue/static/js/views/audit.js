// Audit tab: operator audit log table, config rollback and CSV export.
// Uses app.js globals at call time: tqAct, i18n, showToast, _showPageEmpty, _csvCell.
import { S } from '../core/state.js';
import { escHtml, fmt } from '../util/format.js';
import { fetchJson } from '../core/api.js';
import { projStyle } from '../util/projcolor.js';


export async function loadAudit() {
  const data = await fetchJson('/api/audit?limit=200', 8000, true);
  if (!data) return false;
  S.auditRows = data.events || [];
  const tb = document.getElementById('audit-tbody');
  if (!tb) return;
  _showPageEmpty('audit-empty', 'audit-wrap', S.auditRows.length === 0);
  tb.innerHTML = S.auditRows.map(r => {
    const pj = r.project
      ? `<span class="proj-tag" style="${projStyle(r.project)}">${escHtml(r.project)}</span>`
      : '';
    return `<tr>
    <td>${escHtml(fmt(r.ts))}</td>
    <td>${escHtml(r.actor)}</td>
    <td>${escHtml(r.action)}</td>
    <td>${pj}</td>
    <td>${escHtml(r.target || '')}</td>
    <td>${auditDetailHtml(r)}</td>
  </tr>`;
  }).join('');
}

/** Config events carry JSON {changes:{key:{old,new}}}: render a diff and a rollback button. */
export function auditDetailHtml(r) {
  let ch = null;
  if (r.action === 'config' || r.action === 'config_rollback') {
    try { ch = JSON.parse(r.detail || '').changes; } catch (e) { ch = null; }
  }
  if (!ch) return escHtml(r.detail || '');
  const rows = Object.keys(ch).map(k =>
    `<div><code>${escHtml(k)}</code>: ${escHtml(JSON.stringify(ch[k].old))} → ${escHtml(JSON.stringify(ch[k].new))}</div>`).join('');
  const id = Number(r.id);
  return rows + (r.action === 'config' && id
    ? `<button class="btn btn-sm" data-click="${tqAct([['tqRollbackConfig', id]])}">${escHtml(i18n.t('audit.rollback'))}</button>` : '');
}

export async function tqRollbackConfig(id) {
  if (!confirm(i18n.t('audit.rollbackConfirm'))) return;
  const r = await fetch(`/api/audit/${id}/rollback`, { method: 'POST' });
  const j = await r.json().catch(() => ({}));
  if (!r.ok || !j.ok) { showToast(j.error || ('HTTP ' + r.status), 'err'); return; }
  showToast(i18n.t('audit.rollbackDone') + ': ' + (j.restored || []).join(', '), 'ok');
  loadAudit();
}

export function tqExportAudit() {
  const header = ['ts', 'actor', 'action', 'project', 'target', 'detail'];
  const lines = [header.join(',')];
  for (const r of S.auditRows) {
    lines.push(header.map(k => _csvCell(r[k])).join(','));
  }
  const blob = new Blob([lines.join('\n')], {type: 'text/csv'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'operator-audit.csv';
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}
