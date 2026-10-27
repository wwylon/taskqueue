// Command palette (Ctrl+K / Cmd+K): type to jump to a page, open a task's drawer, or run a
// common action. Items are built on open from the sidebar's data-tab buttons and the loaded queue.
// Uses app.js globals at call time: i18n, tab, tqOpenDrawer, refresh, tqToggleShortcuts.
import { S } from '../core/state.js';
import { escHtml } from '../util/format.js';

let _items = [];
let _sel = 0;
let _returnFocus = null;

function _el() {
  let root = document.getElementById('tq-palette');
  if (root) return root;
  root = document.createElement('div');
  root.id = 'tq-palette';
  root.className = 'tq-palette';
  root.hidden = true;
  root.setAttribute('role', 'dialog');
  root.setAttribute('aria-modal', 'true');
  root.setAttribute('aria-label', i18n.t('palette.title'));
  root.innerHTML = `<div class="tq-palette-panel">
    <input id="tq-palette-input" type="text" autocomplete="off" spellcheck="false"
      role="combobox" aria-controls="tq-palette-list" aria-expanded="true">
    <ul id="tq-palette-list" role="listbox"></ul></div>`;
  document.body.appendChild(root);
  root.addEventListener('click', e => { if (e.target === root) tqClosePalette(); });
  const input = root.querySelector('#tq-palette-input');
  input.addEventListener('input', () => { _sel = 0; _render(input.value); });
  input.addEventListener('keydown', _onKey);
  root.querySelector('#tq-palette-list').addEventListener('click', e => {
    const li = e.target.closest && e.target.closest('li[data-idx]');
    if (li) _run(_filtered(input.value)[Number(li.getAttribute('data-idx'))]);
  });
  return root;
}

/** Everything the palette can do right now: pages, tasks on the loaded queue page, actions. */
export function tqPaletteItems() {
  const items = [];
  document.querySelectorAll('button[data-tab]').forEach(b => {
    const name = b.getAttribute('data-tab');
    const label = (b.textContent || name).trim();
    items.push({ kind: 'page', label, hint: i18n.t('palette.page'), run: () => tab(name, b) });
  });
  (S.queue || []).forEach(t => items.push({
    kind: 'task', label: `${t.project}/${t.task_id}`, hint: String(t.status || ''),
    run: () => tqOpenDrawer(t.project, t.task_id),
  }));
  items.push({ kind: 'action', label: i18n.t('palette.refresh'), hint: i18n.t('palette.action'), run: () => refresh() });
  items.push({ kind: 'action', label: i18n.t('palette.shortcuts'), hint: i18n.t('palette.action'), run: () => tqToggleShortcuts() });
  return items;
}

/** Case-insensitive subsequence match, best (shortest, earliest) first; at most 50 rows. */
export function tqPaletteFilter(items, query) {
  const q = String(query || '').toLowerCase().trim();
  if (!q) return items.slice(0, 50);
  const scored = [];
  for (const it of items) {
    const s = it.label.toLowerCase();
    let pos = -1, first = -1, ok = true;
    for (const ch of q) {
      pos = s.indexOf(ch, pos + 1);
      if (pos < 0) { ok = false; break; }
      if (first < 0) first = pos;
    }
    if (ok) scored.push([s.includes(q) ? 0 : 1, first, s.length, it]);
  }
  scored.sort((a, b) => a[0] - b[0] || a[1] - b[1] || a[2] - b[2]);
  return scored.slice(0, 50).map(x => x[3]);
}

function _filtered(q) { return tqPaletteFilter(_items, q); }

function _render(q) {
  const list = document.getElementById('tq-palette-list');
  if (!list) return;
  const rows = _filtered(q);
  if (_sel >= rows.length) _sel = Math.max(0, rows.length - 1);
  list.innerHTML = rows.length
    ? rows.map((it, i) => `<li data-idx="${i}" role="option" aria-selected="${i === _sel}" class="${i === _sel ? 'sel' : ''}">
        <span class="tq-palette-label">${escHtml(it.label)}</span><span class="tq-palette-hint">${escHtml(it.hint)}</span></li>`).join('')
    : `<li class="empty">${escHtml(i18n.t('palette.none'))}</li>`;
}

function _onKey(e) {
  const input = e.target;
  const rows = _filtered(input.value);
  if (e.key === 'ArrowDown') { _sel = Math.min(rows.length - 1, _sel + 1); _render(input.value); e.preventDefault(); }
  else if (e.key === 'ArrowUp') { _sel = Math.max(0, _sel - 1); _render(input.value); e.preventDefault(); }
  else if (e.key === 'Enter') { if (rows[_sel]) _run(rows[_sel]); e.preventDefault(); }
  else if (e.key === 'Escape') { tqClosePalette(); e.preventDefault(); e.stopPropagation(); }
}

function _run(item) {
  if (!item) return;
  tqClosePalette();
  try { item.run(); } catch (err) { console.error('[tq] palette action failed', err); }
}

export function tqOpenPalette() {
  const root = _el();
  _returnFocus = document.activeElement;
  _items = tqPaletteItems();
  _sel = 0;
  root.hidden = false;
  const input = root.querySelector('#tq-palette-input');
  input.value = '';
  input.placeholder = i18n.t('palette.placeholder');
  _render('');
  input.focus();
}

export function tqClosePalette() {
  const root = document.getElementById('tq-palette');
  if (root) root.hidden = true;
  if (_returnFocus && typeof _returnFocus.focus === 'function') { try { _returnFocus.focus(); } catch (_) {} }
  _returnFocus = null;
}

if (typeof document !== 'undefined' && document.addEventListener) {
  document.addEventListener('keydown', e => {
    if ((e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey && (e.key === 'k' || e.key === 'K')) {
      e.preventDefault();
      const root = document.getElementById('tq-palette');
      if (root && !root.hidden) tqClosePalette(); else tqOpenPalette();
    }
  });
}
