"""Behavior tests for components/prefs.js (theme, font size, language selector, popup)."""
from __future__ import annotations

from tests.test_js_app import run_js
from tests.test_js_smoke import EV


def test_theme_buttons_apply_persist_and_mark_active():
    out = run_js(
        EV
        + """
const { g, doc } = await setup({ app: true });
await g.flush();
const root = doc.documentElement;
const activeOf = sel => doc.querySelectorAll(sel).filter(b => b.classList.has('active')).map(b => b.getAttribute('data-theme-val'));
const r = { boot: [root.getAttribute('data-theme'), root.getAttribute('data-theme-pref'), activeOf("#prefs-theme .seg-opt")] };
fire(doc, 'click', doc.querySelector('#prefs-theme [data-theme-val="light"]'));
r.light = [root.getAttribute('data-theme'), root.getAttribute('data-theme-pref'), g.localStorage.getItem('tq_theme'), activeOf("#prefs-theme .seg-opt")];
g.tqApplyTheme('bogus');                                      // unknown value falls back to dark
r.bogus = [root.getAttribute('data-theme'), g.localStorage.getItem('tq_theme')];
g.matchMedia = () => ({ matches: false });
g.window.matchMedia = g.matchMedia;
g.tqApplyTheme('auto');                                       // system says light
r.auto = [root.getAttribute('data-theme'), root.getAttribute('data-theme-pref')];
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert out["boot"][:2] == ["dark", "dark"] and out["boot"][2] == ["dark"]
    assert out["light"] == ["light", "light", "light", ["light"]]
    assert out["bogus"] == ["dark", "dark"]
    assert out["auto"] == ["light", "auto"]


def test_font_size_validation_and_popup_toggle():
    out = run_js(
        EV
        + """
const { g, doc } = await setup({ app: true });
await g.flush();
const root = doc.documentElement;
const r = {};
fire(doc, 'click', doc.querySelector('#prefs-font [data-font-val="xlarge"]'));
r.xl = [root.getAttribute('data-font'), g.localStorage.getItem('tq_font'),
        doc.querySelectorAll('#prefs-font .seg-opt').filter(b => b.classList.has('active')).map(b => b.getAttribute('data-font-val'))];
g.tqApplyFont('huge');
r.bad = root.getAttribute('data-font');
const pop = doc.getElementById('prefs-pop');
const before = !!pop.hidden;
g.togglePrefs(); const mid = !!pop.hidden; g.togglePrefs();
r.toggle = [before, mid, !!pop.hidden];
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert out["xl"] == ["xlarge", "xlarge", ["xlarge"]] and out["bad"] == "normal"
    assert out["toggle"][0] != out["toggle"][1] and out["toggle"][0] == out["toggle"][2]


def test_sync_prefs_restores_saved_values_and_follows_system_theme_in_auto_mode():
    out = run_js(
        EV
        + """
const { g, doc } = await setup({ app: true });
await g.flush();
const root = doc.documentElement;
g.localStorage.setItem('tq_theme', 'light');
g.localStorage.setItem('tq_font', 'large');
g.localStorage.setItem('tq_lang', 'pl');
const sel = doc.getElementById('tq-lang-switcher');
let onChange = null; let systemDark = false;
g.window.matchMedia = () => ({ get matches() { return systemDark; }, addEventListener(_t, fn) { onChange = fn; } });
g.tqSyncPrefs();
const r = { restored: [root.getAttribute('data-theme'), root.getAttribute('data-font'), sel.value], hasListener: typeof onChange === 'function' };
// in auto mode a system change re-applies the theme; in explicit mode it is ignored
g.tqApplyTheme('auto'); systemDark = true; onChange();
r.auto = root.getAttribute('data-theme');
g.tqApplyTheme('light'); systemDark = true; onChange();
r.explicit = root.getAttribute('data-theme');
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert out["restored"] == ["light", "large", "pl"] and out["hasListener"] is True
    assert out["auto"] == "dark" and out["explicit"] == "light"


def test_operator_prefs_default_mine_and_view_all():
    out = run_js(
        EV
        + """
const { g, doc } = await setup({ app: true });
await g.flush();
const r = {};
g.tqSetOperator({ value: 'ada' });
r.set = [g.tqState.operator, g.tqState.mineOnly, g.localStorage.getItem('tq_operator'), g.localStorage.getItem('tq_mine')];
g.tqSetMineScope(false);
r.viewAll = [g.tqState.mineOnly, g.localStorage.getItem('tq_mine'),
  !!doc.getElementById('prefs-view-all').classList.has('active')];
g.tqSetMineScope(true);
r.mine = [g.tqState.mineOnly, g.localStorage.getItem('tq_mine')];
g.tqSetOperator({ value: '' });
r.clear = [g.tqState.operator, g.tqState.mineOnly];
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert out["set"] == ["ada", True, "ada", "1"]
    assert out["viewAll"] == [False, "0", True]
    assert out["mine"] == [True, "1"]
    assert out["clear"] == ["", False]
