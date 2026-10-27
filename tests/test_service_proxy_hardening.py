import http.server
import json
import threading
from datetime import UTC, datetime

import pytest

from tests.helpers.master_worker import http_request, make_handler_env


class _Up(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/redir"):
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/x")
            self.end_headers()
            return
        self.send_response(404)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"nope")

    def log_message(self, *a):
        pass


@pytest.fixture()
def upstream():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Up)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def _seed(db, upstream_url):
    now = datetime.now(UTC).isoformat()
    db.execute(
        "INSERT INTO tasks (project, task_id, command, status, data, enqueued_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        ("proj", "T1", "x", "running", json.dumps({"services": [{"name": "svc", "upstream": upstream_url}]}), now, now),
    )
    db.execute(
        "INSERT INTO runs (project, task_id, status, started_at) VALUES ('proj','T1','running',?)",
        (now,),
    )
    db.commit()
    return db.execute("SELECT MAX(run_id) FROM runs").fetchone()[0]


def _call(tmp_path, monkeypatch, url, path):
    H, db, _ = make_handler_env(tmp_path)
    rid = _seed(db, url)
    monkeypatch.setattr("taskqueue.data.service_broker.verify_service_token", lambda *a, **k: (True, ""))
    return http_request(H, "GET", f"/api/service/svc{path}", extra_headers={"X-TQ-Service-Token": "t", "X-TQ-Run-Id": str(rid)})


def test_upstream_404_passed_through(tmp_path, monkeypatch, upstream):
    status, _ = _call(tmp_path, monkeypatch, upstream, "/a")
    assert status == 404


def test_redirect_not_followed(tmp_path, monkeypatch, upstream):
    status, _ = _call(tmp_path, monkeypatch, upstream, "/redir")
    assert status == 302


def test_file_scheme_rejected(tmp_path, monkeypatch):
    status, _ = _call(tmp_path, monkeypatch, "file:///etc/passwd", "/a")
    assert status == 502
