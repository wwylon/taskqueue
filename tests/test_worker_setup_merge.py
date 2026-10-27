from __future__ import annotations

import io
import json

import yaml

from taskqueue import worker


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_setup_keeps_local_only_projects_and_extra_keys(tmp_path, monkeypatch):
    home = tmp_path / "w"
    home.mkdir()
    local_root = tmp_path / "localproj"
    shared_root = tmp_path / "shared"
    shared_root.mkdir()
    (home / "projects.yaml").write_text(
        yaml.dump({"projects": {
            "localonly": {"root": str(local_root)},
            "shared": {"root": str(shared_root), "queue_dir": "custom/q"},
        }}),
        encoding="utf-8",
    )
    master = {"shared": {"git_url": None}}
    monkeypatch.setattr(worker.urllib.request, "urlopen",
                        lambda req, timeout=0: _Resp(json.dumps(master).encode()))
    worker.setup_worker(home, "http://m:1", "w1")
    out = yaml.safe_load((home / "projects.yaml").read_text(encoding="utf-8"))["projects"]
    assert out["localonly"]["root"] == str(local_root)
    assert out["shared"]["queue_dir"] == "custom/q"
    assert out["shared"]["root"] == str(shared_root)
