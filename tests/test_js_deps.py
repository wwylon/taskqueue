"""Behavior tests for views/deps.js (Dependencies DAG tab) running under the Node DOM stub."""
from __future__ import annotations

from tests.test_js_app import run_js
from tests.test_js_smoke import EV

NODES = [
    {"id": "p/a", "status": "running", "priority": 50},
    {"id": "p/b", "status": "queued", "priority": 50},
    {"id": "p/c", "status": "failed", "priority": 50, "last_error_code": "tq.block.dep", "block_reason": "dep dead"},
]
EDGES = [{"from": "p/a", "to": "p/b", "health": "ok"}, {"from": "p/b", "to": "p/c", "health": "dead"}]


def test_load_deps_filters_statuses_adds_file_nodes_and_renders_layers():
    out = run_js(
        EV
        + f"""
const nodes = {NODES!r}.map(n => ({{ ...n }}));
nodes[0].transfer = [{{ direction: 'push', dst: 'D:/data/in.csv' }}];
const {{ g, doc, S }} = await setup({{ app: true, routes: {{ '/api/deps': {{ nodes, edges: {EDGES!r} }} }} }});
await g.flush();
const r = {{}};
doc.getElementById('deps-show-files').checked = true;
await g.loadDeps(); await g.flush();
const svg = doc.getElementById('dag-svg');
const ids = () => svg.querySelectorAll('.dag-node').map(n => n.getAttribute('data-id'));
r.withFiles = ids();
r.transferEdges = (svg.innerHTML.match(/stroke-dasharray="5,3"/g) || []).length;
// unchecking a status box hides those nodes and the edges that touch them
doc.getElementById('deps-show-files').checked = false;
const cb = doc.querySelectorAll('.deps-status-cb').find(c => c.dataset.status === 'failed');
r.hasCb = !!cb;
if (cb) cb.checked = false;
await g.loadDeps(); await g.flush();
r.filtered = ids();
r.edgePaths = (svg.innerHTML.match(/<path/g) || []).length;
// layered layout: each dependency sits one column (260 units) to the right of its parent
const x = id => Number((svg.querySelectorAll('.dag-node').find(n => n.getAttribute('data-id') === id).querySelector('rect').getAttribute('x')));
r.layout = [x('p/a'), x('p/b')];
r.click = svg.querySelectorAll('.dag-node').find(n => n.getAttribute('data-id') === 'p/b').getAttribute('data-click');
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert out["withFiles"] == ["p/a", "p/b", "p/c", "file::D:/data/in.csv"] or set(out["withFiles"]) == {"p/a", "p/b", "p/c", "file::D:/data/in.csv"}
    assert out["transferEdges"] == 1
    assert out["hasCb"]
    assert out["filtered"] == ["p/a", "p/b"] or set(out["filtered"]) == {"p/a", "p/b"}
    assert out["edgePaths"] == 1
    assert out["layout"][1] - out["layout"][0] == 260
    assert '"dagNodeClick"' in out["click"] and '"p"' in out["click"]


def test_dag_context_menu_offers_status_specific_actions_and_runs_them():
    out = run_js(
        EV
        + """
const { g, doc } = await setup({ app: true });
await g.flush();
const menuEl = () => doc.body.children.find(c => c.id === 'dag-ctx-menu');
const calls = [];
for (const n of ['stopTask', 'forceTask', 'markProcessed']) g[n] = (btn, p, t) => { calls.push([n, p, t, btn.disabled]); };
g.retryTaskNew = async (p, t, btn) => { calls.push(['retryTaskNew', p, t]); };
const ev = { clientX: 5, clientY: 6, preventDefault() { this.pd = true; }, stopPropagation() {} };
const labels = (status, opts) => {
  g.showDagCtxMenu(ev, 'p', 't', status, opts);
  return menuEl().querySelectorAll('.dag-ctx-item').map(b => b.textContent);
};
const r = { pd: false };
r.running = labels('running');
r.disabled = labels('disabled');
r.timeline = labels('queued', { timeline: true, priority: 40 });
r.pd = ev.pd;
// the menu entries are data-click actions run by the delegated handler
g.showDagCtxMenu(ev, 'p', 't', 'completed');
const menu = menuEl();
const force = menu.querySelectorAll('.dag-ctx-item').find(b => b.textContent === 'Force run');
const mark = menu.querySelectorAll('.dag-ctx-item').find(b => b.textContent === 'Mark done');
fire(doc, 'click', force); fire(doc, 'click', mark);
await g.flush();
r.calls = calls.map(c => c.slice(0, 3));
r.hidden = menu.style.display;
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert out["pd"] is True
    assert out["running"][:2] == ["Go to Queue", "Open YAML"] and "Stop" in out["running"]
    assert "Enable" in out["disabled"] and "Stop" not in out["disabled"]
    assert any("(40)" in label for label in out["timeline"])
    assert ["forceTask", "p", "t"] in out["calls"] and ["markProcessed", "p", "t"] in out["calls"]
    assert out["hidden"] == "none"


def test_dag_zoom_actions_and_node_click_switches_to_queue():
    out = run_js(
        EV
        + """
const { g, doc } = await setup({ app: true, routes: { '/api/deps': { nodes: [{ id: 'p/a', status: 'queued' }], edges: [] } } });
await g.flush();
const r = {};
await g.loadDeps(); await g.flush();
const label = () => doc.getElementById('deps-scale-label').textContent;
g.tqDagZoomBy(0.5);
r.zoomIn = label();
g.tqDagScaleFrom({ value: '0.25' });
r.slider = label();
g.tqDagZoomBy(-5);                      // clamps at the 0.02 minimum
r.min = label();
// clicking a node jumps to the Queue tab for that project
fire(doc, 'click', doc.getElementById('dag-svg').querySelector('.dag-node'));
await g.flush(); await g.flush();
r.page = (doc.querySelector('.page.active') || {}).id;
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert out["slider"] == "0.25×" and out["min"] == "0.02×"
    assert out["zoomIn"].endswith("×")
    assert out["page"] == "queue"
