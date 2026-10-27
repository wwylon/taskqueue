#!/usr/bin/env python3
"""Generate static help bundles and error_labels.json from docs/."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "taskqueue" / "static"
sys.path.insert(0, str(ROOT))

from taskqueue.docs_loader import (  # noqa: E402
    load_manifest,
    validate_manifest,
    write_generated_artifacts,
)


def main() -> int:
    manifest = load_manifest()
    errors = validate_manifest(manifest)
    if errors:
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        return 1
    check = "--check" in sys.argv[1:]
    targets = [STATIC / "help-bundle.en.json", STATIC / "error_labels.json"]
    before = {t: (t.read_bytes() if t.is_file() else None) for t in targets}
    paths = write_generated_artifacts(manifest)
    if check:
        # CI mode: fail when the committed bundle is stale, and leave the files as they were.
        stale = [t.name for t in targets if (t.read_bytes() if t.is_file() else None) != before[t]]
        for t, old in before.items():
            if old is not None:
                t.write_bytes(old)
        if stale:
            print(f"STALE: {', '.join(stale)} — run python scripts/build_docs.py and commit", file=sys.stderr)
            return 1
        print("OK: generated docs are up to date")
        return 0
    print(f"Wrote {paths['help_bundle']}")
    print(f"Wrote {paths['error_labels']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
