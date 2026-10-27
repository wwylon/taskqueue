"""Help topic manifest coverage."""

from __future__ import annotations

import json

from taskqueue.docs_loader import build_help_bundle, load_manifest, validate_manifest, static_dir

UI_TABS = ("queue", "dashboard", "history", "workers", "groups", "deps", "resources", "usage", "config")


def test_manifest_validation():
    assert validate_manifest() == []


def test_page_topic_for_each_tab():
    manifest = load_manifest()
    topics = manifest["topics"]
    for tab in UI_TABS:
        pid = f"page-{tab}" if tab != "config" else "page-config"
        assert pid in topics, f"missing {pid}"


def test_general_topics_registered():
    manifest = load_manifest()
    for tid in manifest["general_topic_ids"]:
        assert tid in manifest["topics"]


def test_help_bundle_not_stale():
    """T-059: committed help-bundle.en.json must match what build_docs.py would generate."""
    committed = json.loads((static_dir() / "help-bundle.en.json").read_text(encoding="utf-8"))
    regenerated = build_help_bundle("en")
    committed_s = json.dumps(committed, ensure_ascii=False, sort_keys=True)
    regenerated_s = json.dumps(regenerated, ensure_ascii=False, sort_keys=True)
    assert committed_s == regenerated_s, (
        "help-bundle.en.json is stale — run: uv run python scripts/build_docs.py"
    )
