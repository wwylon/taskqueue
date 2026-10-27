"""Behavior tests for components/palette.js (Ctrl+K command palette)."""
from __future__ import annotations

from tests.test_js_app import run_js
from tests.test_js_smoke import EV


def test_palette_lists_pages_tasks_and_actions_and_filters():
    out = run_js(EV + """
const { g, doc, S } = await setup({ app: true, routes: {} });
await g.flush();
S.queue = [{ project: 'proj', task_id: 'train_big', status: 'queued' }, { project: 'proj', task_id: 'eval', status: 'failed' }];
const items = g.tqPaletteItems();
const kinds = [...new Set(items.map(i => i.kind))];
const f = g.tqPaletteFilter(items, 'trbig').map(i => i.label);
const opened = []; g.tqOpenDrawer = (p, t) => opened.push([p, t]);
g.tqPaletteFilter(items, 'proj/eval')[0].run();
console.log('RESULT:' + JSON.stringify({ kinds, f, opened, pages: items.filter(i => i.kind === 'page').length }));
""")
    assert out["kinds"] == ["page", "task", "action"]
    assert out["f"][0] == "proj/train_big"
    assert out["opened"] == [["proj", "eval"]]
    assert out["pages"] >= 10
