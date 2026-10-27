// Preferences popup: theme (dark/light/auto), font size, language selector sync, operator + mine scope.
// localStorage keys: tq_theme, tq_font, tq_lang, tq_quiet_hours, tq_operator, tq_mine.
// app.js calls tqSyncPrefs() once at boot.

import { S } from '../core/state.js';

export function tqApplyTheme(pref) {
  let mode = pref || 'dark';
  if (mode !== 'light' && mode !== 'auto') mode = 'dark';
  let resolved = mode;
  if (mode === 'auto') {
    resolved = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }
  document.documentElement.setAttribute('data-theme', resolved === 'light' ? 'light' : 'dark');
  document.documentElement.setAttribute('data-theme-pref', mode);
  try { localStorage.setItem('tq_theme', mode); } catch (e) {}
  document.querySelectorAll('#prefs-theme .seg-opt').forEach(b => {
    b.classList.toggle('active', b.getAttribute('data-theme-val') === mode);
  });
}
export function tqApplyFont(size) {
  if (size !== 'large' && size !== 'xlarge') size = 'normal';
  document.documentElement.setAttribute('data-font', size);
  try { localStorage.setItem('tq_font', size); } catch (e) {}
  document.querySelectorAll('#prefs-font .seg-opt').forEach(b => {
    b.classList.toggle('active', b.getAttribute('data-font-val') === size);
  });
}
export function togglePrefs() {
  const pop = document.getElementById('prefs-pop');
  if (!pop) return;
  pop.hidden = !pop.hidden;
}
export function tqSyncPrefs() {
  let pref = 'dark', font = 'normal', lang = 'en';
  try {
    pref = localStorage.getItem('tq_theme') || 'dark';
    font = localStorage.getItem('tq_font') || 'normal';
    lang = localStorage.getItem('tq_lang') || 'en';
  } catch (e) {}
  tqApplyTheme(pref);
  tqApplyFont(font);
  try {
    const qh = document.getElementById('prefs-quiet');
    if (qh) qh.value = localStorage.getItem('tq_quiet_hours') || '';
  } catch (e) {}
  tqSyncOperatorPrefs();
  const sel = document.getElementById('tq-lang-switcher');
  if (sel) sel.value = lang;
  const mql = window.matchMedia('(prefers-color-scheme: dark)');
  if (mql.addEventListener) {
    mql.addEventListener('change', () => {
      if (document.documentElement.getAttribute('data-theme-pref') === 'auto') tqApplyTheme('auto');
    });
  }
}

/** True when `now` falls in the quiet-hours window "HH:MM-HH:MM" (may wrap midnight). Bad/empty spec = never quiet. */
export function tqInQuietHours(spec, now) {
  const m = /^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$/.exec(String(spec || ''));
  if (!m) return false;
  const a = (+m[1]) * 60 + (+m[2]), b = (+m[3]) * 60 + (+m[4]);
  if (+m[1] > 23 || +m[3] > 23 || +m[2] > 59 || +m[4] > 59 || a === b) return false;
  const cur = now.getHours() * 60 + now.getMinutes();
  return a < b ? (cur >= a && cur < b) : (cur >= a || cur < b);
}

export function tqSetQuietHours(el) {
  const v = (el && el.value || '').trim();
  try { localStorage.setItem('tq_quiet_hours', v); } catch (e) {}
}

/** R8: load operator + mine-scope from localStorage into S and prefs inputs. */
export function tqSyncOperatorPrefs() {
  let op = '', mineStored = null;
  try {
    op = (localStorage.getItem('tq_operator') || '').trim();
    mineStored = localStorage.getItem('tq_mine');
  } catch (e) {}
  S.operator = op;
  // Default mine-on when an operator is set; explicit '0' is View all.
  S.mineOnly = !!op && mineStored !== '0';
  const inp = document.getElementById('prefs-operator');
  if (inp) inp.value = op;
  const mineBtn = document.getElementById('prefs-mine');
  if (mineBtn) {
    mineBtn.classList.toggle('active', S.mineOnly);
    mineBtn.disabled = !op;
    mineBtn.setAttribute('aria-pressed', S.mineOnly ? 'true' : 'false');
  }
  const allBtn = document.getElementById('prefs-view-all');
  if (allBtn) {
    allBtn.classList.toggle('active', !!op && !S.mineOnly);
    allBtn.disabled = !op;
    allBtn.setAttribute('aria-pressed', (!!op && !S.mineOnly) ? 'true' : 'false');
  }
}

export function tqSetOperator(el) {
  const v = (el && el.value || '').trim();
  try { localStorage.setItem('tq_operator', v); } catch (e) {}
  S.operator = v;
  if (!v) {
    S.mineOnly = false;
    try { localStorage.removeItem('tq_mine'); } catch (e) {}
  } else if (localStorage.getItem('tq_mine') == null) {
    // First time setting an operator → default to mine.
    S.mineOnly = true;
    try { localStorage.setItem('tq_mine', '1'); } catch (e) {}
  }
  tqSyncOperatorPrefs();
  if (typeof _populateProjectChips === 'function' && S.projects) _populateProjectChips(S.projects);
  if (typeof refresh === 'function') refresh();
}

export function tqSetMineScope(mine) {
  if (!S.operator) return;
  S.mineOnly = !!mine;
  try { localStorage.setItem('tq_mine', S.mineOnly ? '1' : '0'); } catch (e) {}
  tqSyncOperatorPrefs();
  S.queuePage = 0;
  S.historyPage = 0;
  if (typeof _populateProjectChips === 'function' && S.projects) _populateProjectChips(S.projects);
  if (typeof refresh === 'function') refresh();
  if (!S.applyingUrl && typeof _syncUrl === 'function') _syncUrl({ replace: true });
}
