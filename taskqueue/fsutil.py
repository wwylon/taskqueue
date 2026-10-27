"""Small filesystem helpers shared by the web layer and operator features."""

from __future__ import annotations

import os
import uuid
from pathlib import Path


def atomic_write_text(path: Path | str, text: str, encoding: str = "utf-8") -> None:
    """Write ``text`` to ``path`` via a sibling temp file + ``os.replace``.

    Readers (the YAML scanner) never see a half-written file, and a failure
    leaves the original untouched.
    """
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(tmp, "w", encoding=encoding, newline="") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
