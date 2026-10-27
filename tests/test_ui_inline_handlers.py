"""XSS regression: inline handler arguments must be JS-string-escaped, not just HTML-escaped."""
from __future__ import annotations

import re
from pathlib import Path

APP_JS = Path(__file__).resolve().parent.parent / "taskqueue" / "static" / "app.js"


_INLINE = re.compile(r"""[\s"'`]on(?:click|dblclick|change|input|keydown|keyup|submit|contextmenu|mouse[a-z]+|focus|blur)=["'$]""")


def test_no_inline_event_handler_attributes() -> None:
    """Handlers are data-<event> JSON delegated by js/core/actions.js (arguments are data, never code)."""
    static = APP_JS.parent
    files = [static / "index.html", APP_JS, *sorted((static / "js").rglob("*.js"))]
    bad = []
    for f in files:
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if _INLINE.search(line):
                bad.append(f"{f.name}:{n}")
    assert not bad, f"inline handlers found, use data-<event>=tqAct([[fn, ...args]]): {bad}"


# Behavior-level checks (data-action targets, single SSE stream, status-checked mutations, log cap, fmtCpu)
# live in tests/test_js_app.py, which runs the real app.js under a DOM stub.
