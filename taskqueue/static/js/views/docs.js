// Built-in help drawer and the Docs tab (guide/reference manifest, section nav, in-page links). Uses app.js globals at
// call time: tqAct, i18n, tqCloseShortcuts, tab, _syncUrl. State lives in S.docsCurrentId / S.docsSectionId plus the
// module-local help bundle and docs manifest caches.
import { S } from '../core/state.js';
import { escHtml } from '../util/format.js';

// ---- Built-in help drawer ----
let _helpBundle = null;

export function _activePageTab() {
  const active = document.querySelector('.page.active');
  return (active && active.id) || 'queue';
}
export function _helpUiLang() {
  try { return localStorage.getItem('tq_lang') || 'en'; } catch (_) { return 'en'; }
}
export async function _fetchHelpPayload() {
  const tab = _activePageTab();
  const lang = _helpUiLang();
  try {
    const r = await fetch(`/api/help?tab=${encodeURIComponent(tab)}&lang=${encodeURIComponent(lang)}`);
    if (r.ok) return await r.json();
  } catch (_) {}
  if (!_helpBundle) {
    try {
      const r = await fetch('/static/help-bundle.en.json');
      if (r.ok) _helpBundle = await r.json();
    } catch (_) {}
  }
  if (_helpBundle && _helpBundle.tabs && _helpBundle.tabs[tab]) return _helpBundle.tabs[tab];
  if (_helpBundle && _helpBundle.tabs && _helpBundle.tabs.queue) return _helpBundle.tabs.queue;
  return null;
}
const _HELP_TOPIC_LOCALE = {
  'block-project': ['help.blockTitle', 'help.blockBody'],
  'force-run': ['help.forceTitle', 'help.forceBody'],
  'groups': ['help.groupsTitle', 'help.groupsBody'],
  'dependencies': ['help.depsTitle', 'help.depsBody'],
  'prod-vs-dev': ['help.portsTitle', 'help.portsBody'],
};
export function _helpTopicHtml(topic) {
  if (!topic) return '';
  const lang = _helpUiLang();
  const keys = _HELP_TOPIC_LOCALE[topic.id];
  if (lang !== 'en' && keys) {
    const title = i18n.t(keys[0]);
    const body = i18n.t(keys[1]);
    if (title !== keys[0] && body !== keys[1]) {
      return `<article class="tq-help-topic"><h3>${escHtml(title)}</h3><div class="tq-help-topic-body"><p>${escHtml(body)}</p></div></article>`;
    }
  }
  return `<article class="tq-help-topic"><h3>${escHtml(topic.title)}</h3><div class="tq-help-topic-body">${topic.html || ''}</div></article>`;
}
export async function _renderHelpBody(body) {
  const payload = await _fetchHelpPayload();
  if (!payload) {
    body.innerHTML = `<p>${escHtml(i18n.t('help.loadFailed'))}</p>`;
    return;
  }
  let html = '';
  if (payload.page) {
    html += _helpTopicHtml(payload.page);
    if (payload.page.read_more) {
      const rm = String(payload.page.read_more);
      const hashIdx = rm.indexOf('#');
      const guideId = 'web-ui';
      const anchor = hashIdx >= 0 ? rm.slice(hashIdx + 1) : '';
      const openArgs = anchor
        ? `'${guideId}','${escHtml(anchor)}'`
        : `'${guideId}'`;
      html += `<p class="tq-help-more"><a href="#" data-click="${tqAct([['tqOpenGuide', openArgs]])}" data-click-prevent>${escHtml(i18n.t('help.readMore'))}</a></p>`;
    }
  }
  if (payload.general && payload.general.length) {
    html += `<h3 class="tq-help-section-label">${escHtml(i18n.t('help.allTopics'))}</h3>`;
    for (const t of payload.general) html += _helpTopicHtml(t);
  }
  html += `<p class="tq-help-footer"><a href="/api/docs" target="_blank" rel="noopener">${escHtml(i18n.t('help.apiDocs'))}</a></p>`;
  body.innerHTML = html;
}
export async function openHelp() {
  tqCloseShortcuts();
  const tab = _activePageTab();
  let existing = document.getElementById('tq-help-modal');
  if (existing) {
    const wasHidden = existing.style.display === 'none';
    existing.style.display = 'flex';
    if (wasHidden || existing.dataset.helpTab !== tab) {
      existing.dataset.helpTab = tab;
      const body = existing.querySelector('.tq-help-body');
      if (body) await _renderHelpBody(body);
    }
    return;
  }
  const m = document.createElement('div');
  m.id = 'tq-help-modal';
  m.className = 'tq-help-overlay';
  m.dataset.helpTab = tab;
  m.onclick = (e) => { if (e.target === m) closeHelp(); };
  m.innerHTML = `<div class="tq-help-panel" role="dialog" aria-label="Help">
    <div class="tq-help-header">
      <strong>${i18n.t('help.title')}</strong>
      <button class="cfg-btn" data-click="${tqAct([['closeHelp']])}">✕</button>
    </div>
    <div class="tq-help-body"><p>${escHtml(i18n.t('help.loading'))}</p></div>
  </div>`;
  document.body.appendChild(m);
  await _renderHelpBody(m.querySelector('.tq-help-body'));
}
export function closeHelp() {
  const m = document.getElementById('tq-help-modal');
  if (m) m.style.display = 'none';
}

let _docsManifest = null;

export function tqOpenGuide(id, section) {
  closeHelp();
  tqCloseShortcuts();
  S.docsCurrentId = id || 'web-ui';
  S.docsSectionId = section || null;
  const btn = document.querySelector('#side-nav button[data-tab="docs"]');
  if (btn) tab('docs', btn);
  else loadDoc(S.docsCurrentId, { section: S.docsSectionId });
}

export async function loadDocsPage() {
  if (!_docsManifest) {
    try {
      const r = await fetch('/docs/manifest.json');
      if (r.ok) _docsManifest = await r.json();
    } catch (_) {}
  }
  if (!_docsManifest) return;
  const gNav = document.getElementById('docs-nav-guides');
  const rNav = document.getElementById('docs-nav-refs');
  if (!gNav || !rNav) return;
  gNav.innerHTML = Object.entries(_docsManifest.guides || {}).map(([id]) =>
    `<button type="button" class="docs-nav-item" data-doc-id="${escHtml(id)}" data-click="${tqAct([['loadDoc', String(id)]])}">${escHtml(id.replace(/-/g, ' '))}</button>`
  ).join('');
  rNav.innerHTML = Object.entries(_docsManifest.references || {}).map(([id]) =>
    `<button type="button" class="docs-nav-item" data-doc-id="${escHtml(id)}" data-click="${tqAct([['loadDoc', String(id)]])}">${escHtml(id.replace(/-/g, ' '))}</button>`
  ).join('');
  let pick = S.docsCurrentId || 'web-ui';
  try {
    const saved = localStorage.getItem('tq_last_doc');
    if (!S.docsCurrentId && saved) pick = saved;
  } catch (_) {}
  loadDoc(pick, { replace: true, section: S.docsSectionId });
}

export function _docsScrollToSection(sectionId) {
  if (!sectionId) return;
  const content = document.getElementById('docs-content');
  if (!content) return;
  const el = content.querySelector('#' + CSS.escape(sectionId));
  if (!el) return;
  el.scrollIntoView({ block: 'start', behavior: 'auto' });
}

export function _docsRenderSectionNav(content) {
  let host = document.getElementById('docs-nav-sections');
  let label = document.getElementById('docs-sections-label');
  if (!host) {
    const sidebar = document.getElementById('docs-sidebar');
    if (!sidebar) return;
    label = document.createElement('div');
    label.id = 'docs-sections-label';
    label.className = 'docs-sidebar-sub';
    label.textContent = (i18n.t && i18n.t('docs.onThisPage') !== 'docs.onThisPage')
      ? i18n.t('docs.onThisPage') : 'On this page';
    host = document.createElement('div');
    host.id = 'docs-nav-sections';
    const refsLabel = [...sidebar.querySelectorAll('.docs-sidebar-sub')].find(el => el.id !== 'docs-sections-label');
    if (refsLabel) {
      sidebar.insertBefore(label, refsLabel);
      sidebar.insertBefore(host, refsLabel);
    } else {
      sidebar.appendChild(label);
      sidebar.appendChild(host);
    }
  }
  const sections = [...content.querySelectorAll('section[id]')];
  if (!sections.length) {
    host.innerHTML = '';
    if (label) label.hidden = true;
    return;
  }
  if (label) label.hidden = false;
  host.innerHTML = sections.map(sec => {
    const id = sec.id;
    const h = sec.querySelector('h2, h3');
    const title = (h && h.textContent) ? h.textContent.trim() : id;
    const active = id === S.docsSectionId ? ' active' : '';
    return `<button type="button" class="docs-nav-item${active}" data-section-id="${escHtml(id)}" data-click="${tqAct([['tqDocsJump', String(id)]])}">${escHtml(title)}</button>`;
  }).join('');
}

export function tqDocsJump(sectionId) {
  S.docsSectionId = sectionId || null;
  if (!S.applyingUrl) _syncUrl({ replace: false });
  _docsScrollToSection(sectionId);
  document.querySelectorAll('#docs-nav-sections .docs-nav-item').forEach(el => {
    el.classList.toggle('active', el.dataset.sectionId === sectionId);
  });
}

export async function loadDoc(docId, opts) {
  opts = opts || {};
  const content = document.getElementById('docs-content');
  const title = document.getElementById('docs-title');
  if (!content) return;
  if (opts.section !== undefined) S.docsSectionId = opts.section || null;
  else if (S.docsCurrentId !== docId) S.docsSectionId = null;
  // Same guide already shown — keep scroll unless a section jump was requested.
  if (
    S.docsCurrentId === docId
    && content.dataset.docId === docId
    && content.querySelector('section, h1, h2, p')
    && !opts.force
    && opts.section === undefined
  ) {
    return;
  }
  const sectionJumpOnly = (
    S.docsCurrentId === docId
    && content.dataset.docId === docId
    && opts.section
    && !opts.force
  );
  if (sectionJumpOnly) {
    if (!S.applyingUrl) _syncUrl({ replace: !!opts.replace });
    _docsRenderSectionNav(content);
    _docsScrollToSection(S.docsSectionId);
    return;
  }
  S.docsCurrentId = docId;
  if (!S.applyingUrl) _syncUrl({ replace: !!opts.replace });
  content.innerHTML = `<p>${escHtml(i18n.t('status.loading'))}</p>`;
  document.querySelectorAll('.docs-nav-item[data-doc-id]').forEach(el => {
    el.classList.toggle('active', el.dataset.docId === docId);
  });
  try {
    const r = await fetch(`/docs/${encodeURIComponent(docId)}?format=json`);
    if (!r.ok) throw new Error('not found');
    const doc = await r.json();
    if (title) title.textContent = doc.title || docId;
    content.innerHTML = doc.html || '';
    content.dataset.docId = docId;
    _docsRenderSectionNav(content);
    content.querySelectorAll('a[href^="/docs/"]').forEach(a => {
      a.addEventListener('click', (ev) => {
        const href = a.getAttribute('href') || '';
        const m = href.match(/^\/docs\/([^?#]+)(?:\?[^#]*)?(?:#(.+))?$/);
        if (!m) return;
        ev.preventDefault();
        S.docsSectionId = m[2] || null;
        loadDoc(m[1], { section: S.docsSectionId, force: true });
      });
    });
    if (S.docsSectionId) {
      requestAnimationFrame(() => _docsScrollToSection(S.docsSectionId));
    } else {
      content.scrollTop = 0;
    }
    try { localStorage.setItem('tq_last_doc', docId); } catch (_) {}
  } catch (_) {
    content.innerHTML = `<p>${escHtml(i18n.t('docs.loadFailed'))}</p>`;
    content.dataset.docId = '';
  }
}
