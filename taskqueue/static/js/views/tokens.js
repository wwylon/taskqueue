// API tokens tab. Uses app.js globals at call time: tqAct, i18n, showToast, _postJson, _sendJson, _showPageEmpty.
import { escHtml, fmt } from '../util/format.js';
import { fetchJson } from '../core/api.js';

export async function loadApiTokens() {
  const data = await fetchJson('/api/tokens', 8000, true);
  if (!data) return false;
  const rows = data.tokens || [];
  const tb = document.getElementById('tokens-tbody');
  if (!tb) return;
  _showPageEmpty('tokens-empty', 'tokens-wrap', rows.length === 0);
  tb.innerHTML = rows.map(r => `<tr>
    <td>${escHtml(r.name)}</td>
    <td class="task-id-mono">${escHtml(r.prefix)}</td>
    <td>${escHtml(r.scope)}${r.projects && r.projects.length ? ' · ' + escHtml(r.projects.join(', ')) : ''}</td>
    <td>${escHtml(fmt(r.created_at))}</td>
    <td>${escHtml(r.last_used_at ? fmt(r.last_used_at) : '—')}</td>
    <td>${escHtml(r.revoked_at ? fmt(r.revoked_at) : '')}</td>
    <td>${r.revoked_at ? '' : `<button type="button" class="cfg-btn" data-click="${tqAct([['tqRevokeToken', Number(r.id)]])}">${escHtml(i18n.t('api.revoke'))}</button>`}</td>
  </tr>`).join('');
}

export function tqCreateToken(ev) {
  if (ev) ev.preventDefault();
  _postJson('/api/tokens', {
    name: document.getElementById('token-name').value.trim(),
    scope: document.getElementById('token-scope').value,
    projects: (document.getElementById('token-projects').value || '').trim(),
  }).then(data => {
    const once = document.getElementById('token-once');
    if (once) {
      once.hidden = false;
      once.textContent = i18n.t('api.once') + ' ' + (data.token || '');
    }
    const name = document.getElementById('token-name');
    if (name) name.value = '';
    loadApiTokens();
  }).catch(err => showToast(String(err.message || err), 'err'));
  return false;
}

export function tqRevokeToken(id) {
  _sendJson('DELETE', '/api/tokens/' + encodeURIComponent(id))
    .then(() => loadApiTokens())
    .catch(err => showToast(String(err.message || err), 'err'));
}
