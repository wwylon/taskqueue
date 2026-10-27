// Accessibility attributes derived from state the UI already tracks in CSS classes, so no render path needs to change.

/** `.modal-bg > .modal` without a role becomes a labelled modal dialog. */
export function annotateDialogs(root) {
  root.querySelectorAll('.modal-bg > .modal:not([role])').forEach(m => {
    m.setAttribute('role', 'dialog');
    m.setAttribute('aria-modal', 'true');
    const h = m.querySelector('h2[id]');
    if (h) m.setAttribute('aria-labelledby', h.id);
  });
}

/** `aria-pressed` mirrors `.active` on filter chips; `aria-sort` mirrors `.sort-asc|.sort-desc` on sortable headers. */
export function syncStateAttrs(root) {
  root.querySelectorAll('.filter-chip').forEach(b => {
    const on = b.classList.contains('active') ? 'true' : 'false';
    if (b.getAttribute('aria-pressed') !== on) b.setAttribute('aria-pressed', on);
  });
  root.querySelectorAll('th.sortable').forEach(th => {
    const v = th.classList.contains('sort-asc') ? 'ascending' : th.classList.contains('sort-desc') ? 'descending' : 'none';
    if (th.getAttribute('aria-sort') !== v) th.setAttribute('aria-sort', v);
    const hasTab = typeof th.hasAttribute === 'function'
      ? th.hasAttribute('tabindex')
      : th.getAttribute('tabindex') != null;
    if (!hasTab) th.setAttribute('tabindex', '0');
  });
  // F-135: icon-only mobile nav needs accessible names from the label span.
  root.querySelectorAll('#side-nav button[data-tab]').forEach(btn => {
    const label = typeof btn.querySelector === 'function' ? btn.querySelector('.side-label') : null;
    const text = label && (label.textContent || '').trim();
    if (text && btn.getAttribute('aria-label') !== text) btn.setAttribute('aria-label', text);
  });
}

export function installA11y(doc) {
  if (typeof MutationObserver === 'undefined' || typeof requestAnimationFrame === 'undefined' || !doc.body) return;
  annotateDialogs(doc);
  syncStateAttrs(doc);
  let queued = false;
  const run = () => { queued = false; annotateDialogs(doc); syncStateAttrs(doc); };
  new MutationObserver(() => { if (!queued) { queued = true; requestAnimationFrame(run); } })
    .observe(doc.body, { subtree: true, childList: true, attributes: true, attributeFilter: ['class'] });
}
