"""Behavior tests for views/taskstats.js (Tasks tab table, search debounce, empty states)."""
from __future__ import annotations

from tests.test_js_app import run_js
from tests.test_js_smoke import EV

ROWS = [
    {"project": "alpha", "task_id": "train-<1>", "last_finish": None, "mean_sec": 60, "p50_sec": 50, "p95_sec": 90, "count": 4, "success_rate": 0.75},
    {"project": "beta", "task_id": "eval", "last_finish": None, "mean_sec": 5, "p50_sec": 5, "p95_sec": 6, "count": 1, "success_rate": 1},
]

PRE = """
const { g, doc, S } = await setup({ app: true, routes: ROUTES });
await g.flush();
for (const id of ['tasks-tbody', 'tasks-search', 'tasks-search-clear', 'tasks-empty', 'tasks-filter-empty', 'tasks-wrap']) if (!doc.getElementById(id)) doc.add(id);
const $ = id => doc.getElementById(id);
const r = {};
"""


def _js(routes: str, body: str) -> str:
    return EV + PRE.replace("ROUTES", routes) + body + "\nconsole.log('RESULT:' + JSON.stringify(r));\n"


def test_load_renders_rows_escapes_and_keeps_last_good_rows_on_failure():
    import json
    out = run_js(_js("{ '/api/task-stats': { tasks: %s } }" % json.dumps(ROWS), """
await g.loadTaskStats();
const html = () => $('tasks-tbody').innerHTML;
r.rows = [(html().match(/<tr>/g) || []).length, html().includes('train-&lt;1&gt;'), html().includes('75%'), html().includes('tqOpenDrawer')];
r.proj = html().includes('class="proj-tag"') && html().includes('alpha');
r.chrome = [$('tasks-empty').hidden, $('tasks-filter-empty').hidden, $('tasks-wrap').style.display];
const url = g.fetches.filter(f => f.url.startsWith('/api/task-stats')).pop().url;
r.url = url;
g.fetch = async () => { throw new Error('down'); };
await g.loadTaskStats();
r.kept = S.taskStatsRows.length;
S.taskStatsRows = []; g._renderTaskStatsTable();
r.empty = [$('tasks-empty').hidden, $('tasks-wrap').style.display];
"""))
    assert out["rows"] == [2, True, True, True] and out["proj"] is True
    assert out["chrome"] == [True, True, ""]
    assert out["url"].startswith("/api/task-stats")
    assert out["kept"] == 2 and out["empty"] == [False, "none"]


def test_search_filters_debounces_and_clear_button_resets():
    out = run_js(_js("{}", """
S.taskStatsRows = %s;
$('tasks-search').value = 'ALP';
g.onTasksSearchInput();
r.pending = [$('tasks-search').classList.has('tq-searching'), $('tasks-search-clear').hidden, (($('tasks-tbody').innerHTML.match(/<tr>/g)) || []).length];
g.onTasksSearchInput();                                          // second keystroke replaces the timer
r.timers = g.timers.filter(t => t.live && t.ms === 250).length;
g.fireTimers(250);
r.filtered = [$('tasks-search').classList.has('tq-searching'), (($('tasks-tbody').innerHTML.match(/<tr>/g)) || []).length, S.tasksSearchDebounce];
$('tasks-search').value = 'nomatch'; g._renderTaskStatsTable();
r.noMatch = [$('tasks-filter-empty').hidden, $('tasks-empty').hidden, $('tasks-wrap').style.display];
g.clearTasksSearch();
r.cleared = [$('tasks-search').value, $('tasks-search-clear').hidden, (($('tasks-tbody').innerHTML.match(/<tr>/g)) || []).length];
""" % __import__("json").dumps(ROWS)))
    assert out["pending"] == [True, False, 0] and out["timers"] == 1
    assert out["filtered"] == [False, 1, None]
    assert out["noMatch"] == [False, True, "none"]
    assert out["cleared"] == ["", True, 2]
