// Dependencies (DAG) tab. Uses app.js globals at call time: tqAct, TQ_THIS, TQ_EVENT, i18n, friendlyErrorText,
// setProjectAndWait, tab, _flashQueueRow, loadTimeline, stopTask, retryTaskNew, forceTask, markProcessed, enableTask,
// openYamlModal, tqOpenDrawer, tqPromptPriority, tqSetPriority, tqSetEnabled, _isRestartStatus, _requeueLabel, _postJson.
import { STATUS_HEX, hexToRgba } from '../util/colors.js';
import { projColor } from '../util/projcolor.js';
import { S } from '../core/state.js';
import { escHtml } from '../util/format.js';

export async function loadDeps() {
  const allProjectsEl = document.getElementById('deps-all-projects');
  // If a specific project is selected globally and the checkbox hasn't been explicitly checked, filter by it.
  const allProjects = allProjectsEl ? allProjectsEl.checked : (S.selectedProject === 'all');
  const proj = (!allProjects && S.selectedProject && S.selectedProject !== 'all') ? S.selectedProject : '';
  const qs = proj ? `?project=${encodeURIComponent(proj)}` : '';
  try {
    const data = await fetch('/api/deps' + qs).then(r => r.ok ? r.json() : null);
    if (!data) return false;  // keep the last graph; tqLoop backs off
    // status filter checkboxes: a status whose box is unchecked is hidden (statuses without a box stay)
    const hiddenStatuses = new Set();
    document.querySelectorAll('.deps-status-cb').forEach(cb => { if (!cb.checked) hiddenStatuses.add(cb.dataset.status); });
    if (hiddenStatuses.size) {
      data.nodes = (data.nodes || []).filter(n => !hiddenStatuses.has(n.status));
      const kept = new Set(data.nodes.map(n => n.id));
      data.edges = (data.edges || []).filter(e => kept.has(e.from) && kept.has(e.to));
    }
    // W7.7: inject file nodes from transfer specs
    const showFiles = (document.getElementById('deps-show-files') || {}).checked;
    if (showFiles) {
      const fileNodes = {};
      const extraEdges = [];
      for (const n of (data.nodes || [])) {
        if (!n.transfer || !n.transfer.length) continue;
        for (const t of n.transfer) {
          const path = t.dst || t.src || '';
          if (!path) continue;
          const fileId = 'file::' + path;
          if (!fileNodes[fileId]) {
            fileNodes[fileId] = {id: fileId, status: 'file', description: path, priority: 50, type: 'file'};
          }
          if (t.direction === 'push') {
            // file → task (master pushes file to worker before task runs)
            extraEdges.push({from: fileId, to: n.id, transfer: true, direction: 'push'});
          } else {
            // task → file (worker pulls file back after completion)
            extraEdges.push({from: n.id, to: fileId, transfer: true, direction: 'pull'});
          }
        }
      }
      data.nodes = (data.nodes || []).concat(Object.values(fileNodes));
      data.edges = (data.edges || []).concat(extraEdges);
    }
    renderDag(data);
  } catch(e) {
    const svg = document.getElementById('dag-svg');
    if (svg) svg.innerHTML = `<text x="50%" y="50%" text-anchor="middle" fill="var(--fail)" font-size="13">Error: ${escHtml(String(e))}</text>`;
  }
}

// W6.16: pan/zoom state
let _dagView = {x: 0, y: 0, scale: 1};
let _dagDrag = null;
let _dagUserMoved = false;
let _dagPointers = new Map();
let _dagPinch = null;
let _dagLastDragMoved = false;

export function _dagScaleLabel(scale) {
  return (scale < 1 ? scale.toFixed(2) : scale.toFixed(1)) + '×';
}

export function _dagResetView() {
  _dagUserMoved = false;
  _dagView = {x: 0, y: 0, scale: 1};
  // Re-render re-applies auto-fit on fresh view state
  loadDeps();
}

// Content size in graph units; the SVG is sized to this times the scale so the container scrolls natively.
let _dagBounds = {w: 0, h: 0};

/** Default view: scale so the graph fills the container width; the height scrolls. */
export function _dagFitToView(totalW, totalH) {
  const container = document.getElementById('dag-container');
  if (!container || totalW <= 0 || totalH <= 0) return;
  const cr = container.getBoundingClientRect();
  if (cr.width < 20 || cr.height < 20) return;
  _dagBounds = {w: totalW, h: totalH};
  // Leave room for a vertical scrollbar so the width fit does not also trigger a horizontal one.
  const fitScale = Math.min(_DAG_MAX_SCALE, Math.max(0.001, (cr.width - 2 - 16) / totalW));
  _dagView.scale = fitScale;
  _dagView.x = 0;
  _dagView.y = 0;
  _dagApplyTransform();
  const slider = document.getElementById('deps-scale');
  if (slider) {
    if (fitScale > parseFloat(slider.max)) slider.max = String(fitScale);
    slider.value = String(fitScale);
  }
}

if (typeof window !== 'undefined' && window.addEventListener) window.addEventListener('resize', function() {
  if (_dagUserMoved) return;
  const page = document.getElementById('deps');
  const g = document.getElementById('dag-g');
  if (!page || !page.classList.contains('active') || !g) return;
  const bb = g.getBBox();
  if (!bb.width || !bb.height) return;
  _dagFitToView(bb.x + bb.width + 20, bb.y + bb.height + 20);
});

const _DAG_MAX_SCALE = 2;
// W9.6: slider-driven zoom
export function _dagApplyScale(scale) {
  _dagUserMoved = true;
  // Zoom around the centre of the visible area.
  const c = document.getElementById('dag-container');
  const old = _dagView.scale || 1;
  if (c) {
    _dagView.x = -c.scrollLeft;
    _dagView.y = -c.scrollTop;
    const cx = c.clientWidth / 2, cy = c.clientHeight / 2;
    _dagView.x = cx - (cx - _dagView.x) * (scale / old);
    _dagView.y = cy - (cy - _dagView.y) * (scale / old);
  }
  _dagView.scale = scale;
  _dagApplyTransform();
}

let _dagSyncingScroll = false;
/** Size the SVG to the scaled graph and scroll the container to the view offset (x, y <= 0). */
export function _dagApplyTransform() {
  const g = document.getElementById('dag-g');
  const svg = document.getElementById('dag-svg');
  const c = document.getElementById('dag-container');
  const s = _dagView.scale;
  if (g) g.setAttribute('transform', `scale(${s})`);
  if (svg && c && _dagBounds.w) {
    svg.style.width = Math.max(_dagBounds.w * s, c.clientWidth) + 'px';
    svg.style.height = Math.max(_dagBounds.h * s, c.clientHeight) + 'px';
    _dagSyncingScroll = true;
    c.scrollLeft = -_dagView.x;
    c.scrollTop = -_dagView.y;
    _dagView.x = -c.scrollLeft;
    _dagView.y = -c.scrollTop;
    _dagSyncingScroll = false;
  }
  const lbl = document.getElementById('deps-scale-label');
  if (lbl) lbl.textContent = _dagScaleLabel(s);
}

export function _setupDagPanZoom(svg, container) {
  container.onscroll = function() {
    if (_dagSyncingScroll) return;
    _dagView.x = -container.scrollLeft;
    _dagView.y = -container.scrollTop;
  };
  // Plain wheel / shift-wheel scroll the graph; Ctrl (or pinch-on-trackpad) + wheel zooms.
  svg.onwheel = function(e) {
    if (!(e.ctrlKey || e.metaKey)) return;
    e.preventDefault();
    const rect = container.getBoundingClientRect();
    const mx = e.clientX - rect.left, my = e.clientY - rect.top;
    const factor = e.deltaY < 0 ? 1.12 : (1/1.12);
    const newScale = Math.max(0.02, Math.min(_DAG_MAX_SCALE, _dagView.scale * factor));
    _dagView.x = mx - (mx - _dagView.x) * (newScale / _dagView.scale);
    _dagView.y = my - (my - _dagView.y) * (newScale / _dagView.scale);
    _dagView.scale = newScale;
    _dagUserMoved = true;
    _dagApplyTransform();
  };

  container.style.touchAction = 'none';
  container.style.cursor = 'grab';

  container.onpointerdown = function(e) {
    // Scrollbars belong to the browser; the SVG always covers the rest of the container.
    if (e.target === container) return;
    _dagView.x = -container.scrollLeft;
    _dagView.y = -container.scrollTop;
    // Primary button / touch / pen only
    if (e.pointerType === 'mouse' && e.button !== 0) return;
    e.preventDefault();
    try { container.setPointerCapture(e.pointerId); } catch (_) {}
    _dagPointers.set(e.pointerId, {x: e.clientX, y: e.clientY});
    if (_dagPointers.size === 1) {
      _dagDrag = {sx: e.clientX, sy: e.clientY, ox: _dagView.x, oy: _dagView.y, moved: false};
      _dagPinch = null;
      container.style.cursor = 'grabbing';
    } else if (_dagPointers.size === 2) {
      const pts = [..._dagPointers.values()];
      const dx = pts[0].x - pts[1].x, dy = pts[0].y - pts[1].y;
      _dagPinch = {dist: Math.hypot(dx, dy) || 1, scale: _dagView.scale};
      _dagDrag = null;
    }
  };

  container.onpointermove = function(e) {
    if (!_dagPointers.has(e.pointerId)) return;
    _dagPointers.set(e.pointerId, {x: e.clientX, y: e.clientY});
    if (_dagPointers.size >= 2 && _dagPinch) {
      const pts = [..._dagPointers.values()];
      const dx = pts[0].x - pts[1].x, dy = pts[0].y - pts[1].y;
      const dist = Math.hypot(dx, dy) || 1;
      const newScale = Math.max(0.02, Math.min(_DAG_MAX_SCALE, _dagPinch.scale * (dist / _dagPinch.dist)));
      const rect = container.getBoundingClientRect();
      const mx = (pts[0].x + pts[1].x) / 2 - rect.left;
      const my = (pts[0].y + pts[1].y) / 2 - rect.top;
      if (_dagView.scale > 0) {
        _dagView.x = mx - (mx - _dagView.x) * (newScale / _dagView.scale);
        _dagView.y = my - (my - _dagView.y) * (newScale / _dagView.scale);
      }
      _dagView.scale = newScale;
      _dagUserMoved = true;
      _dagLastDragMoved = true;
      _dagApplyTransform();
      return;
    }
    if (!_dagDrag) return;
    const dx = e.clientX - _dagDrag.sx, dy = e.clientY - _dagDrag.sy;
    if (Math.hypot(dx, dy) > 6) _dagDrag.moved = true;
    _dagView.x = _dagDrag.ox + dx;
    _dagView.y = _dagDrag.oy + dy;
    _dagUserMoved = true;
    _dagApplyTransform();
  };

  function endPointer(e) {
    if (!_dagPointers.has(e.pointerId)) return;
    _dagPointers.delete(e.pointerId);
    try { container.releasePointerCapture(e.pointerId); } catch (_) {}
    if (_dagPointers.size < 2) _dagPinch = null;
    if (_dagPointers.size === 0) {
      if (_dagDrag) {
        _dagLastDragMoved = !!_dagDrag.moved;
        _dagDrag = null;
      }
      container.style.cursor = 'grab';
    } else if (_dagPointers.size === 1) {
      const p = [..._dagPointers.values()][0];
      _dagDrag = {sx: p.x, sy: p.y, ox: _dagView.x, oy: _dagView.y, moved: true};
    }
  }
  container.onpointerup = endPointer;
  container.onpointercancel = endPointer;
  container.onpointerleave = function(e) {
    // Only end if we lost capture (pointer already gone from map)
    if (_dagPointers.has(e.pointerId) && !container.hasPointerCapture(e.pointerId)) {
      endPointer(e);
    }
  };

  // After a pan/pinch, swallow the synthetic click so a node under the finger
  // does not navigate away. Capture phase runs before node onclick handlers.
  if (!container._dagClickGuard) {
    container.addEventListener('click', function(e) {
      if (!_dagLastDragMoved) return;
      e.preventDefault();
      e.stopPropagation();
      _dagLastDragMoved = false;
    }, true);
    container._dagClickGuard = true;
  }
}

export function renderDag(data) {
  const svg = document.getElementById('dag-svg');
  const tooltip = document.getElementById('dag-tooltip');
  const container = document.getElementById('dag-container');
  if (!svg) return;

  const nodes = data.nodes || [];
  const edges = data.edges || [];

  if (nodes.length === 0) {
    svg.innerHTML = `<text x="50%" y="60" text-anchor="middle" fill="var(--muted)" font-size="13">${i18n.t('deps.noTasks')}</text>`;
    svg.setAttribute('height', 120);
    return;
  }

  // Server caps unfocused graphs; tell the operator to narrow the view.
  const capNote = document.getElementById('dag-cap-note') || (container && container.parentElement
    ? container.parentElement.insertBefore(Object.assign(document.createElement('div'), {id: 'dag-cap-note', className: 'muted'}), container)
    : null);
  if (capNote) {
    capNote.textContent = data.truncated
      ? `Showing ${nodes.length} of ${data.total_nodes} tasks — pick a project, suite or focus task to see the rest.`
      : '';
  }

  // Topological sort into layers
  const incoming = {};
  const outgoing = {};
  for (const n of nodes) { incoming[n.id] = []; outgoing[n.id] = []; }
  for (const e of edges) {
    if (incoming[e.to] !== undefined) incoming[e.to].push(e.from);
    if (outgoing[e.from] !== undefined) outgoing[e.from].push(e.to);
  }

  const layers = [];
  const assigned = {};
  const queue = nodes.filter(n => incoming[n.id].length === 0).map(n => n.id);
  let iter = 0;
  while (queue.length && iter++ < 5000) {
    const id = queue.shift();
    if (assigned[id] !== undefined) continue;
    const parentLayer = incoming[id].reduce((mx, p) => Math.max(mx, assigned[p] !== undefined ? assigned[p] : -1), -1);
    assigned[id] = parentLayer + 1;
    if (!layers[assigned[id]]) layers[assigned[id]] = [];
    layers[assigned[id]].push(id);
    for (const child of (outgoing[id] || [])) queue.push(child);
  }
  // Any node not yet assigned goes to last layer
  for (const n of nodes) {
    if (assigned[n.id] === undefined) {
      const layer = layers.length;   // capture before push extends the array
      assigned[n.id] = layer;
      if (!layers[layer]) layers[layer] = [];
      layers[layer].push(n.id);
    }
  }

  const NODE_W = 200, NODE_H = 34, GAP_X = 60, GAP_Y = 18, PAD = 20;
  const nodePos = {};
  for (const layer of layers) {
    for (let i = 0; i < layer.length; i++) {
      nodePos[layer[i]] = { x: 0, y: PAD + i * (NODE_H + GAP_Y) };
    }
  }
  for (let li = 0; li < layers.length; li++) {
    for (const id of layers[li]) {
      nodePos[id].x = PAD + li * (NODE_W + GAP_X);
    }
  }

  // B1: STATUS_COLOR with stroke+fill
  const STATUS_COLOR = Object.fromEntries(Object.entries(STATUS_HEX).map(([k, hex]) =>
    [k, { stroke: hex, fill: hexToRgba(hex, k === 'disabled' || k === 'missing' ? 0.08 : 0.10) }]));

  // B3: sort nodes within layers by STATUS_ORDER then priority desc
  const STATUS_ORDER = {running:1,queued:2,waiting:2,stopped:3,interrupted:4,failed:5,unknown:5,completed:6,disabled:7};
  const nodeMap = {};
  for (const n of nodes) nodeMap[n.id] = n;
  for (const layer of layers) {
    layer.sort((a, b) => {
      const na = nodeMap[a], nb = nodeMap[b];
      const oa = STATUS_ORDER[na && na.status] || 99;
      const ob = STATUS_ORDER[nb && nb.status] || 99;
      if (oa !== ob) return oa - ob;
      return (nb && nb.priority || 50) - (na && na.priority || 50);
    });
  }

  // Recompute node positions after sort
  for (const layer of layers) {
    for (let i = 0; i < layer.length; i++) {
      if (nodePos[layer[i]]) nodePos[layer[i]].y = PAD + i * (NODE_H + GAP_Y);
    }
  }

  let svgHtml = '';

  // Draw edges
  for (const e of edges) {
    const from = nodePos[e.from], to = nodePos[e.to];
    if (!from || !to) continue;
    const x1 = from.x + NODE_W, y1 = from.y + NODE_H / 2;
    const x2 = to.x, y2 = to.y + NODE_H / 2;
    const cx = (x1 + x2) / 2;
    // W7.7: dashed transfer edges — push=blue, pull=green
    if (e.transfer) {
      const clr = e.direction === 'push' ? '#60a5fa' : '#34d399';
      svgHtml += `<path d="M${x1},${y1} C${cx},${y1} ${cx},${y2} ${x2},${y2}" fill="none" stroke="${clr}" stroke-width="1.5" stroke-dasharray="5,3" opacity="0.75"/>`;
    } else {
      const clr = e.health === 'ok' ? '#34d399' : e.health === 'dead' ? '#e74c3c' : e.health === 'wait' ? '#f59e0b' : null;
      const w = e.health === 'dead' ? '2.5' : '1.5';
      if (clr) {
        svgHtml += `<path d="M${x1},${y1} C${cx},${y1} ${cx},${y2} ${x2},${y2}" fill="none" stroke="${clr}" stroke-width="${w}"/>`;
      } else {
        svgHtml += `<path class="dag-edge" d="M${x1},${y1} C${cx},${y1} ${cx},${y2} ${x2},${y2}"/>`;
      }
    }
  }

  // Draw nodes
  for (const n of nodes) {
    const pos = nodePos[n.id];
    if (!pos) continue;
    // W7.7: file nodes rendered as diamonds
    if (n.type === 'file') {
      const cx = pos.x + NODE_W / 2, cy = pos.y + NODE_H / 2;
      const hw = NODE_W / 2 - 4, hh = NODE_H / 2;
      const pts = `${cx},${cy - hh} ${cx + hw},${cy} ${cx},${cy + hh} ${cx - hw},${cy}`;
      const fname = n.description.split(/[\\/]/).pop();
      const flabel = fname.length > 20 ? fname.slice(0, 18) + '…' : fname;
      svgHtml += `<g class="dag-node" data-id="${escHtml(n.id)}" data-desc="${escHtml(n.description)}" data-status="file"
          data-mouseenter="${tqAct([['showDagTip', TQ_EVENT, TQ_THIS]])}" data-mouseleave="${tqAct([['hideDagTip']])}">
        <polygon points="${pts}" fill="rgba(6,182,212,0.12)" stroke="#06b6d4" stroke-width="1.5"/>
        <text x="${cx}" y="${cy + 4}" font-size="10" fill="#06b6d4" text-anchor="middle">${escHtml(flabel)}</text>
      </g>`;
      continue;
    }
    const sc = STATUS_COLOR[n.status] || {stroke:'#6b7280', fill:'rgba(107,114,128,0.08)'};
    const parts = n.id.split('/');
    const nodeProj = parts.length > 1 ? parts[0] : (S.selectedProject !== 'all' ? S.selectedProject : '');
    const nodeTid = parts.length > 1 ? parts[1] : parts[0];
    const numPrefix = n.yaml_num ? `#${n.yaml_num} ` : '';
    const rawLabel = numPrefix + nodeTid;
    const label = rawLabel.length > 22 ? rawLabel.slice(0, 20) + '…' : rawLabel;
    const descEsc = escHtml(n.description || '');
    const idEsc = escHtml(n.id);
    // G2: show block reason for tq.block.* error codes
    const errCode = n.last_error_code || '';
    const friendly = friendlyErrorText(errCode, n.block_reason || '');
    const hasBlock = !!(errCode || (n.block_reason || ''));
    const blockText = hasBlock ? friendly.slice(0, 28) : '';
    const nodeH = hasBlock ? NODE_H + 16 : NODE_H;
    const dash = (n.stub || n.status === 'missing') ? ' stroke-dasharray="4,3"' : '';
    // B4: dagNodeClick with project and task_id; right-click opens action menu
    svgHtml += `<g class="dag-node" data-id="${idEsc}" data-desc="${descEsc}" data-status="${escHtml(n.status||'')}"
        data-mouseenter="${tqAct([['showDagTip', TQ_EVENT, TQ_THIS]])}" data-mouseleave="${tqAct([['hideDagTip']])}"
        data-click="${tqAct([['dagNodeClick', String(nodeProj), String(nodeTid)]])}"
        data-contextmenu="${tqAct([['showDagCtxMenu', TQ_EVENT, String(nodeProj), String(nodeTid), n.status || '']])}">
      <rect x="${pos.x}" y="${pos.y}" width="${NODE_W}" height="${nodeH}"
        rx="6" fill="${sc.fill}" stroke="${sc.stroke}" stroke-width="2"${dash}/>
      ${nodeProj ? `<rect x="${pos.x + 2}" y="${pos.y + 6}" width="4" height="${nodeH - 12}" rx="2" fill="${projColor(nodeProj)}"/>` : ''}
      <text x="${pos.x + 8}" y="${pos.y + 13}" font-size="10" fill="${sc.stroke}" font-weight="600">${escHtml(n.status||'')}${nodeProj ? `<tspan fill="${projColor(nodeProj)}" font-weight="500"> · ${escHtml(nodeProj.length > 18 ? nodeProj.slice(0, 16) + '…' : nodeProj)}</tspan>` : ''}</text>
      <text x="${pos.x + 8}" y="${pos.y + 26}" font-size="12" fill="var(--text)">${escHtml(label)}</text>
      ${hasBlock ? `<text x="${pos.x + 8}" y="${pos.y + nodeH - 4}" font-size="10" fill="#f59e0b">${escHtml(blockText)}</text>` : ''}
    </g>`;
  }

  let boundsW = PAD;
  let boundsH = PAD;
  for (const n of nodes) {
    const pos = nodePos[n.id];
    if (!pos) continue;
    const nh = (n.type !== 'file' && (n.last_error_code || n.block_reason)) ? NODE_H + 16 : NODE_H;
    boundsW = Math.max(boundsW, pos.x + NODE_W + PAD);
    boundsH = Math.max(boundsH, pos.y + nh + PAD);
  }
  // SVG fills the full container; content is panned/zoomed via the inner <g>
  svg.removeAttribute('width'); svg.removeAttribute('height'); svg.removeAttribute('viewBox');
  svg.innerHTML = `<g id="dag-g" transform="scale(${_dagView.scale})">${svgHtml}</g>`;
  _setupDagPanZoom(svg, container);
  _dagBounds = {w: boundsW, h: boundsH};
  if (!_dagUserMoved) _dagFitToView(boundsW, boundsH);
  else _dagApplyTransform();

  // W6.16: status colour legend below chart
  const legendEl = document.getElementById('dag-legend');
  if (legendEl) {
    const STATUS_COLOR_LEGEND = ['running', 'queued', 'waiting', 'completed', 'failed', 'interrupted',
      'suspended', 'stopped', 'cancelled', 'disabled'].map(s => [s, STATUS_HEX[s]]);
    const usedStatuses = new Set(nodes.map(n => n.status));
    legendEl.innerHTML = STATUS_COLOR_LEGEND
      .filter(([s]) => usedStatuses.has(s))
      .map(([s, c]) => `<span style="display:inline-flex;align-items:center;gap:4px"><span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:${c};opacity:0.85"></span>${s}</span>`)
      .join('');
  }
}

export function showDagTip(event, el) {
  const tip = document.getElementById('dag-tooltip');
  if (!tip) return;
  const id = el.dataset.id || '';
  const status = el.dataset.status || '';
  const desc = el.dataset.desc || '';
  tip.innerHTML = `<div style="font-weight:600;margin-bottom:4px">${escHtml(id)}</div>`
    + `<div style="color:var(--muted)">status: ${escHtml(status)}</div>`
    + (desc ? `<div style="margin-top:4px;color:var(--text2)">${escHtml(desc)}</div>` : '');
  tip.style.display = 'block';
  const container = document.getElementById('dag-container');
  const cr = container ? container.getBoundingClientRect() : {left:0,top:0};
  tip.style.left = (event.clientX - cr.left + (container ? container.scrollLeft : 0) + 12) + 'px';
  tip.style.top = (event.clientY - cr.top + (container ? container.scrollTop : 0) + 12) + 'px';
}

export function hideDagTip() {
  const tip = document.getElementById('dag-tooltip');
  if (tip) tip.style.display = 'none';
}

export async function dagNodeClick(projOrId, taskId) {
  // B4: support both dagNodeClick(project, taskId) and old dagNodeClick(id)
  let proj, tid;
  if (taskId !== undefined) {
    proj = projOrId; tid = taskId;
  } else {
    const parts = projOrId.split('/');
    proj = parts.length > 1 ? parts[0] : (S.selectedProject !== 'all' ? S.selectedProject : '');
    tid = parts.length > 1 ? parts[1] : parts[0];
  }
  if (proj && S.selectedProject !== proj) {
    await setProjectAndWait(proj);
  }
  const qbtn = Array.from(document.querySelectorAll('nav button')).find(b =>
    b.getAttribute('data-i18n') === 'nav.queue' || b.textContent.trim().toLowerCase() === 'queue'
  );
  if (qbtn) tab('queue', qbtn);
  _flashQueueRow(proj, tid, { smooth: false });
}

export function _ephemeralActionBtn() {
  return { disabled: false, textContent: '', _origText: null, _pendingTimer: null };
}

export function hideDagCtxMenu() {
  const m = document.getElementById('dag-ctx-menu');
  if (m) m.style.display = 'none';
}

export function _dagRefreshAfterAction() {
  setTimeout(() => {
    if (document.getElementById('timeline') && document.getElementById('timeline').classList.contains('active')) {
      loadTimeline();
    }
    if (document.getElementById('deps') && document.getElementById('deps').classList.contains('active')) {
      loadDeps();
    }
  }, 700);
}

export function _dagCtxMenuItems(project, taskId, status, opts) {
  const items = [];
  if (opts && opts.timeline) {
    const pr = Number(opts.priority) || 0;
    items.push({ label: i18n.t('ctx.details') || 'Details', call: ['tqOpenDrawer', project, taskId] });
    items.push({ label: (i18n.t('ctx.setPriority') || 'Set priority…') + ' (' + pr + ')', call: ['tqPromptPriority', project, taskId,pr] });
    items.push({ label: (i18n.t('ctx.prioUp') || 'Priority +10'), call: ['tqSetPriority', project, taskId,Math.min(100, pr + 10)] });
    items.push({ label: (i18n.t('ctx.prioDown') || 'Priority −10'), call: ['tqSetPriority', project, taskId,Math.max(0, pr - 10)] });
    items.push({ sep: true });
  }
  items.push({ label: 'Go to Queue', call: ['dagNodeClick', project, taskId] });
  items.push({ label: 'Open YAML', call: ['openYamlModal', project, taskId] });
  const st = status || '';
  const actionItems = [];
  if (st === 'running' || st === 'queued' || st === 'waiting') {
    actionItems.push({ label: 'Stop', call: ['dagCtxStop', project, taskId] });
  }
  if (_isRestartStatus(st) || ['failed', 'completed'].includes(st)) {
    actionItems.push({ label: _requeueLabel(st), call: ['dagCtxRetry', project, taskId] });
  }
  if (['queued', 'waiting', 'stopped', 'failed', 'interrupted', 'suspended', 'cancelled', 'completed'].includes(st)) {
    actionItems.push({ label: 'Force run', call: ['dagCtxForce', project, taskId] });
  }
  if (st === 'completed') {
    actionItems.push({ label: 'Mark done', call: ['dagCtxMarkProcessed', project, taskId] });
  }
  if (st === 'disabled') {
    actionItems.push({ label: 'Enable', call: ['dagCtxEnable', project, taskId] });
  } else if (opts && opts.timeline && ['queued', 'stopped', 'interrupted', 'failed'].includes(st)) {
    actionItems.push({ label: i18n.t('ctx.disable') || 'Disable', call: ['tqSetEnabled', project, taskId,0] });
  }
  if (actionItems.length) {
    items.push({ sep: true });
    items.push(...actionItems);
  }
  return items;
}

export function showDagCtxMenu(event, project, taskId, status, opts) {
  event.preventDefault();
  event.stopPropagation();
  hideDagTip();
  let menu = document.getElementById('dag-ctx-menu');
  if (!menu) {
    menu = document.createElement('div');
    menu.id = 'dag-ctx-menu';
    menu.className = 'dag-ctx-menu';
    document.body.appendChild(menu);
    document.addEventListener('click', hideDagCtxMenu);
    document.addEventListener('contextmenu', (e) => {
      if (!menu.contains(e.target) && !(e.target.closest && e.target.closest('.dag-node'))) {
        hideDagCtxMenu();
      }
    });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') hideDagCtxMenu(); });
    document.addEventListener('scroll', hideDagCtxMenu, true);
  }
  const items = _dagCtxMenuItems(project, taskId, status, opts);
  menu.innerHTML = items.map(it => {
    if (it.sep) return '<div class="dag-ctx-sep"></div>';
    return `<button type="button" class="dag-ctx-item" data-click="${tqAct([['hideDagCtxMenu'], it.call])}" data-click-stop>${escHtml(it.label)}</button>`;
  }).join('');
  menu.style.display = 'block';
  menu.style.left = '0px';
  menu.style.top = '0px';
  const r = menu.getBoundingClientRect();
  let left = event.clientX;
  let top = event.clientY;
  if (left + r.width > window.innerWidth - 8) left = window.innerWidth - r.width - 8;
  if (top + r.height > window.innerHeight - 8) top = window.innerHeight - r.height - 8;
  menu.style.left = Math.max(8, left) + 'px';
  menu.style.top = Math.max(8, top) + 'px';
}

export function dagCtxStop(project, taskId) {
  stopTask(_ephemeralActionBtn(), project, taskId);
  _dagRefreshAfterAction();
}
export function dagCtxRetry(project, taskId) {
  Promise.resolve(retryTaskNew(project, taskId, _ephemeralActionBtn())).finally(_dagRefreshAfterAction);
}
export function dagCtxForce(project, taskId) {
  forceTask(_ephemeralActionBtn(), project, taskId);
  _dagRefreshAfterAction();
}
export function dagCtxMarkProcessed(project, taskId) {
  Promise.resolve(markProcessed(_ephemeralActionBtn(), project, taskId)).finally(_dagRefreshAfterAction);
}
export function dagCtxEnable(project, taskId) {
  Promise.resolve(enableTask(project, taskId, _ephemeralActionBtn())).finally(_dagRefreshAfterAction);
}


export function tqDagZoomBy(delta) {
  _dagApplyScale(delta < 0 ? Math.max(0.02, _dagView.scale + delta) : Math.min(_DAG_MAX_SCALE, _dagView.scale + delta));
}
export function tqDagScaleFrom(el) { _dagApplyScale(parseFloat(el.value)); }
