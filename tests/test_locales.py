"""Locale sanity checks (glm.md §5.7) — not full key parity (legacy help keys differ)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_LOCALES = Path(__file__).resolve().parents[1] / "taskqueue" / "static" / "locales"

# Keys operators see often; must exist in every locale.
_REQUIRED = (
    "header.fontNormal",
    "queue.moreRows",
    "groups.btnViewQueue",
    "toast.projectUnblocked",
)


@pytest.mark.parametrize("lang", ["en", "ar", "pl", "zh"])
def test_locale_required_keys(lang: str) -> None:
    data = json.loads((_LOCALES / f"{lang}.json").read_text(encoding="utf-8"))
    missing = [k for k in _REQUIRED if k not in data]
    assert not missing, f"{lang}.json missing required keys: {missing}"


@pytest.mark.parametrize("lang", ["en", "ar", "pl", "zh"])
def test_locale_no_debug_key_suffix(lang: str) -> None:
    data = json.loads((_LOCALES / f"{lang}.json").read_text(encoding="utf-8"))
    bad = [k for k, v in data.items() if isinstance(v, str) and "[key:" in v]
    assert not bad, f"{lang}.json has debug suffixes: {bad}"


def test_pl_unblocked_not_blocked() -> None:
    data = json.loads((_LOCALES / "pl.json").read_text(encoding="utf-8"))
    assert "zablokowany" not in data["toast.projectUnblocked"].lower()


def test_font_normal_is_aa() -> None:
    for lang in ("en", "ar", "pl", "zh"):
        data = json.loads((_LOCALES / f"{lang}.json").read_text(encoding="utf-8"))
        assert data["header.fontNormal"] == "Aa"


_STATIC = _LOCALES.parent


def _used_keys() -> set[str]:
    """Literal i18n keys the UI uses (data-i18n* attributes and i18n.t('...') calls)."""
    import re

    html = (_STATIC / "index.html").read_text(encoding="utf-8")
    js = "\n".join(f.read_text(encoding="utf-8") for f in [_STATIC / "app.js", *sorted((_STATIC / "js").rglob("*.js"))])
    keys = set(re.findall(r'data-i18n(?:-[a-z]+)?="([A-Za-z0-9_.]+)"', html + js))
    keys |= set(re.findall(r"i18n\.t\(\s*'([A-Za-z0-9_.]*[A-Za-z0-9_])'", js))
    return keys


@pytest.mark.parametrize("lang", ["en", "ar", "pl", "zh"])
def test_every_used_key_is_translated(lang: str) -> None:
    """i18n.t() has no English fallback: a missing key renders as the raw key (e.g. "queue.thFinished")."""
    data = json.loads((_LOCALES / f"{lang}.json").read_text(encoding="utf-8"))
    missing = sorted(k for k in _used_keys() if k not in data)
    assert not missing, f"{lang}.json missing keys used by the UI: {missing}"
