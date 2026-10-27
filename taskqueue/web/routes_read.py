"""Handler mixin (extracted from make_handler)."""
from __future__ import annotations
from taskqueue._time import parse_iso as _parse_iso

# Broad shared namespace so method bodies keep working with minimal churn.
from .deps import (
    Path,
    SCHEMA_VERSION,
    UTC,
    _GIT_SHA,
    _STATIC,
    _attach_log_field,
    _attach_progress_health,
    _build_api_docs_html,
    _get_cost_response,
    _get_deps_response,
    _get_groups_response,
    _get_history_response,
    _get_queue_response,
    _get_suites_response,
    _get_usage_response,
    _history_row_dict,
    _queue_row_dict,
    _redact_config,
    _slim_status_payload,
    _task_dict,
    build_diagnose,
    build_report,
    count_queued_tasks,
    count_system_alert_firing,
    datetime,
    get_config,
    get_running_tasks,
    hashlib,
    html,
    json,
    list_alerts,
    list_attention,
    list_audit,
    list_schedules,
    list_system_alerts,
    list_tokens,
    lock_contention_summary,
    lock_occupancy,
    os,
    ram_used_gib,
    re,
    sqlite3,
    status_counts,
    task_stats,
)
import threading
import time
from typing import Any
from taskqueue._db_tasks import bulk_prior_run_seconds, get_task
from taskqueue.rows import row_get
from taskqueue.web.responses import format_server_timing

# Live fields (elapsed_sec, idle_sec, daemon_*) are patched by SSE on the client; ETag ignores them.
# D9 / F-146: cache _get_workers per DB path to avoid per-request datetime parses.
_workers_cache_by_db: dict[str, tuple[float, list]] = {}
_workers_cache_ttl = 1.0
_SQL_IN_CHUNK = 400  # F-147: stay under SQLite's ~999 bind-variable limit
# Secrets / fencing tokens that must never leave the master via the read API.
_WORKER_SECRET_COLS = frozenset({"credential_hash", "session_id", "instance_id"})

# /sw.js cache version: [monotonic ts, hash]; the static tree is rescanned at most every 5 s.
_SW_VERSION_CACHE: list = []

# lock_contention_summary keyed by change-tracker token (db_path → (token, result)).
_LOCK_CONTENTION_CACHE: dict[str, tuple[Any, Any]] = {}

# Coalesce identical /api/dashboard builds under load (GIL + SQLite contention).
_DASH_CACHE_TTL_SEC = 0.8
_dash_cache_lock = threading.Lock()
# cache_key → (monotonic_ts, etag, uncompressed_body_bytes, phases)
_dash_cache: dict[str, tuple[float, str, bytes, dict[str, float]]] = {}
# cache_key → Event set when the leader finishes (waiters wake and re-read cache).
_dash_inflight: dict[str, threading.Event] = {}


def _dashboard_path_key(path: str) -> str:
    """Request path without ``timing=`` so profilers share the coalesced body/ETag."""
    if "?" not in path:
        return path
    base, qs = path.split("?", 1)
    parts = [p for p in qs.split("&") if p and not p.startswith("timing=")]
    return base + ("?" + "&".join(parts) if parts else "")


class ReadRoutes:
    def _serve_favicon(self) -> None:
        svg = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><text y="26" font-size="28">\xf0\x9f\x93\x8b</text></svg>'
        self.send_response(200)
        self.send_header("Content-Type", "image/svg+xml")
        self.send_header("Content-Length", str(len(svg)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(svg)

    def _serve_service_worker(self) -> None:
        """Serve /sw.js (root scope) with a cache version derived from the static asset sizes+mtimes."""
        import hashlib

        src = _STATIC / "sw.js"
        if not src.exists():
            self.send_error(404)
            return
        now = time.monotonic()
        cached = _SW_VERSION_CACHE
        if cached and now - cached[0] < 5.0:
            version = cached[1]
        else:
            h = hashlib.sha1()
            for f in sorted(p for p in _STATIC.rglob("*") if p.is_file() and p.suffix in (".js", ".css", ".html", ".json")):
                st = f.stat()
                h.update(f"{f.name}:{st.st_size}:{st.st_mtime_ns}".encode())
            version = h.hexdigest()[:12]
            _SW_VERSION_CACHE[:] = [now, version]
        body = src.read_text(encoding="utf-8").replace("__TQ_VERSION__", version).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/javascript")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_locale(self, path: str) -> None:
        fname = path[len("/static/locales/"):]
        if re.fullmatch(r"[A-Za-z0-9_-]+\.json", fname):
            self._serve_file(_STATIC / "locales" / fname, "application/json")
        else:
            self.send_error(404)

    def _serve_js_module(self, path: str) -> None:
        """ES modules under static/js/. Only plain relative *.js paths; no traversal."""
        rel = path[len("/static/js/"):]
        if re.fullmatch(r"[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*\.js", rel):
            self._serve_file(_STATIC / "js" / rel, "text/javascript; charset=utf-8")
        else:
            self.send_error(404)

    def _get_tags_route(self) -> None:
        _tqs = self._query()
        self._json(self._get_tags(_tqs.get("project", [None])[0]))

    def _get_project_roots_route(self) -> None:
        roots = {}
        for name, proj in self._projects_ref().items():
            roots[name] = str(proj.root) if hasattr(proj, "root") else ""
        self._json(roots)

    def _get_my_projects_route(self) -> None:
        """R8: projects where operator started or submitted a task."""
        from taskqueue.queries import list_operator_projects

        qs = self._query()
        op = (qs.get("operator", [None])[0] or "").strip()
        if not op:
            self._json({"operator": "", "projects": []})
            return
        self._json({"operator": op, "projects": list_operator_projects(self._rconn(), operator=op)})

    def _serve_help_api(self) -> None:
        from taskqueue.docs_loader import build_help_payload
        qs = self._query()
        tab = (qs.get("tab", ["queue"])[0] or "queue").strip()
        lang = (qs.get("lang", ["en"])[0] or "en").strip()
        try:
            self._json(build_help_payload(tab, lang))
        except Exception as exc:
            self._send_error_json(500, f"help error: {exc}")

    def _serve_docs(self, path: str) -> None:
        from taskqueue.docs_loader import load_manifest, render_guide, render_reference
        qs = self._query()
        want_json = qs.get("format", [""])[0] == "json" or "application/json" in (self.headers.get("Accept") or "")
        rel = path[len("/docs/"):].lstrip("/")
        if not rel or rel == "manifest.json":
            self._json(load_manifest())
            return
        doc_id = rel.split("/")[0].split(".")[0]
        manifest = load_manifest()
        try:
            if doc_id in manifest.get("guides", {}):
                doc = render_guide(doc_id)
            elif doc_id in manifest.get("references", {}):
                doc = render_reference(doc_id)
            else:
                self.send_error(404)
                return
        except FileNotFoundError:
            self.send_error(404)
            return
        if want_json:
            self._json(doc)
            return
        title = html.escape(doc.get("title", doc_id))
        body = doc.get("html", "")
        page = f"""<!doctype html><html><head><meta charset=utf-8>
<title>{title} — taskqueue</title>
<style>
body{{font-family:system-ui,sans-serif;margin:2rem;max-width:720px;line-height:1.5;
background:#1a1a1a;color:#ddd}} a{{color:#8df}} code{{background:#2a2a2a;padding:2px 4px}}
h1,h2,h3{{color:#eee}} section{{margin:1.2rem 0}}
ul{{padding-left:1.35rem;margin:0.5rem 0}} li{{margin:0.2rem 0}}
</style></head><body>
<h1>{title}</h1>
{body}
<p><a href="/">← UI</a> · <a href="/api/docs">API</a></p>
</body></html>"""
        raw = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _serve_api_docs_html(self) -> None:
        body = _build_api_docs_html().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------------
    # Extracted POST helpers (routes with non-trivial inline logic)
    # ------------------------------------------------------------------

    def _project_colors(self, cfg: dict[str, Any]) -> dict[str, str]:
        """Stored per-project colors, with a deterministic hash color for not-yet-assigned projects."""
        from taskqueue.project_colors import hash_color, load_project_colors

        out = load_project_colors(cfg)
        for p in self._get_all_projects():
            out.setdefault(p, hash_color(p))
        return out

    def _get_status(self, timings: dict[str, float] | None = None) -> dict[str, Any]:  # noqa: C901
        _t0 = time.perf_counter()
        cfg = get_config(self._rconn())
        qs = self._query()
        count_project = (qs.get("project", [None])[0] or "").strip() or None
        counts = status_counts(self._rconn(), project=count_project)
        running_rows = get_running_tasks(self._rconn(), project=count_project)

        # Bulk-fetch last run for all running tasks in one query (avoids N+1).
        # F-147: chunk keys so large fleets stay under SQLite's bind-variable limit.
        running_keys = [(r["project"], r["task_id"]) for r in running_rows]
        last_runs: dict[tuple, Any] = {}
        conn = self._rconn()
        for i in range(0, len(running_keys), _SQL_IN_CHUNK):
            chunk = running_keys[i : i + _SQL_IN_CHUNK]
            placeholders = ",".join("(?,?)" for _ in chunk)
            params = [v for pair in chunk for v in pair]
            rows = conn.execute(
                f"SELECT r.* FROM runs r "
                f"INNER JOIN ("
                f"  SELECT project, task_id, MAX(run_id) AS max_run_id "
                f"  FROM runs WHERE (project, task_id) IN ({placeholders}) "
                f"  GROUP BY project, task_id"
                f") latest ON r.run_id = latest.max_run_id",
                params,
            ).fetchall()
            for r in rows:
                last_runs[(r["project"], r["task_id"])] = r

        # Bulk-fetch worker last_seen for all worker_ids referenced by last runs.
        worker_ids = list({r["worker_id"] for r in last_runs.values() if r["worker_id"]})
        worker_seen: dict[str, Any] = {}
        for i in range(0, len(worker_ids), _SQL_IN_CHUNK):
            chunk = worker_ids[i : i + _SQL_IN_CHUNK]
            ph2 = ",".join("?" for _ in chunk)
            for wr in conn.execute(
                f"SELECT worker_id, last_seen FROM workers WHERE worker_id IN ({ph2})",
                chunk,
            ).fetchall():
                worker_seen[wr["worker_id"]] = wr["last_seen"]

        open_for_prior: dict[tuple[str, str], Any] = {}
        for row in running_rows:
            run = self._active_runs.get((row["project"], row["task_id"]))
            last_run = last_runs.get((row["project"], row["task_id"]))
            rid = run.run_id if run is not None else (last_run["run_id"] if last_run else None)
            if rid is not None:
                open_for_prior[(row["project"], row["task_id"])] = {"run_id": rid}
        priors = bulk_prior_run_seconds(self._rconn(), open_for_prior) if open_for_prior else {}

        running = []
        for row in running_rows:
            d = _task_dict(row, strip_secrets=True)
            run = self._active_runs.get((row["project"], row["task_id"]))
            last_run = last_runs.get((row["project"], row["task_id"]))
            d["started_at"] = last_run["started_at"] if last_run else None

            if run:
                d["elapsed_sec"] = round(run.elapsed_sec(), 1)
                d["progress_pct"] = run.progress_pct
                d["last_log_line"] = run.last_log_line
                d["idle_sec"] = round(run.idle_sec(), 1)
                d["active_idle_sec"] = round(run.active_idle_sec(), 1)
                d["run_id"] = run.run_id
                d["peak_ram_gib"] = round(run.peak_ram_gib, 2)
                d["peak_vram_gib"] = round(run.peak_vram_gib, 2)
                d["peak_cpu_pct"] = round(run.peak_cpu_pct, 1)
                d["current_ram_gib"] = round(run.current_ram_gib, 2)
                d["current_vram_gib"] = round(run.current_vram_gib, 2)
                d["current_cpu_pct"] = round(run.current_cpu_pct, 1)
                if getattr(run, "current_gpu_util_pct", None) is not None:
                    d["current_gpu_util_pct"] = round(run.current_gpu_util_pct, 1)
                if getattr(run, "gpu_util_ema", None) is not None:
                    d["avg_gpu_util_pct"] = round(run.gpu_util_ema, 1)
                _attach_log_field(d, run=run)
            if not run and last_run:
                d["run_id"] = last_run["run_id"]
                d["worker_id"] = last_run["worker_id"]
                d["progress_pct"] = last_run["progress_pct"]
                d["last_log_line"] = last_run["last_log_line"]
                d["peak_ram_gib"] = round(last_run["peak_ram_gib"] or 0.0, 2)
                d["peak_vram_gib"] = round(last_run["peak_vram_gib"] or 0.0, 2)
                d["peak_cpu_pct"] = round(last_run["peak_cpu_pct"] or 0.0, 1)
                if last_run["started_at"]:
                    try:
                        s = _parse_iso(last_run["started_at"])
                        d["elapsed_sec"] = round(max(0.0, (datetime.now(UTC) - s).total_seconds()), 1)
                    except Exception:
                        d["elapsed_sec"] = 0.0
                mj = row_get(last_run, "metrics_json")
                _attach_log_field(d, metrics_json=mj)
            _attach_progress_health(d, run=run, metrics_json=row_get(last_run, "metrics_json") if last_run else None)
            d["prior_elapsed_sec"] = priors.get((row["project"], row["task_id"]), 0.0)
            # J1: worker last-seen age — looked up from bulk fetch above
            wid = d.get("worker_id")
            if wid and wid in worker_seen and worker_seen[wid]:
                try:
                    last_seen = _parse_iso(worker_seen[wid])
                    d["worker_last_seen_age_sec"] = round((datetime.now(UTC) - last_seen).total_seconds(), 1)
                except Exception:
                    pass
            running.append(d)

        live_sys = dict(self._system_info)
        live_sys["ram_used_gib"] = round(ram_used_gib(), 1)

        # W3.6: include package version, schema version and git sha in status.
        try:
            from taskqueue import __version__ as _ver
        except Exception:
            _ver = "unknown"
        # W4.12: daemon self-budget
        daemon_cpu_pct: float | None = None
        daemon_rss_mb: float | None = None
        try:
            import psutil as _psutil
            import os as _os
            _proc = _psutil.Process(_os.getpid())
            daemon_cpu_pct = round(_proc.cpu_percent(interval=None), 1)
            daemon_rss_mb = round(_proc.memory_info().rss / 1_048_576, 1)
        except Exception:
            pass
        if timings is not None:
            timings["status"] = round((time.perf_counter() - _t0) * 1000.0)
        _t_health = time.perf_counter()
        health = self._get_health()
        if timings is not None:
            timings["health"] = round((time.perf_counter() - _t_health) * 1000.0)
        return {
            "running": running,
            "queued_count": count_queued_tasks(self._rconn(), project=count_project),
            "system": live_sys,
            "config": _redact_config(cfg),
            "projects": self._get_all_projects(),
            "project_colors": self._project_colors(cfg),
            "version": _ver,
            "schema_version": SCHEMA_VERSION,
            "git_sha": _GIT_SHA,
            "daemon_cpu_pct": daemon_cpu_pct,
            "daemon_rss_mb": daemon_rss_mb,
            "status_counts": counts,
            "alert_firing": count_system_alert_firing(self._rconn()),
            "health": health,
        }

    def _get_health(self) -> dict[str, Any]:
        """Queue-wide red/amber/green verdict (taskqueue.health); the daemon serving this is up."""
        from taskqueue.health import compute_health, read_ack
        from taskqueue.queries import tasks_cached

        conn = self._rconn()
        ack_at = read_ack(Path(self._db_path).parent)
        # Fingerprint-cache: dashboard/SSE refresh often; invalidate when tasks change.
        return tasks_cached(
            conn,
            f"health|{ack_at or ''}",
            None,
            lambda: compute_health(conn, ack_at=ack_at, daemon_up=True),
            ttl=10.0,
        )

    def _lock_contention_cached(self, change_token: Any) -> Any:
        """Reuse lock_contention_summary while the DB change token is unchanged."""
        key = str(self._db_path)
        hit = _LOCK_CONTENTION_CACHE.get(key)
        if hit is not None and hit[0] == change_token and change_token is not None:
            return hit[1]
        result = lock_contention_summary(self._rconn())
        _LOCK_CONTENTION_CACHE[key] = (change_token, result)
        return result

    def _send_dashboard_bytes(
        self,
        *,
        etag: str,
        body: bytes,
        phases: dict[str, float] | None,
        status: int = 200,
    ) -> None:
        if status == 304:
            self.send_response(304)
            self.send_header("ETag", f'"{etag}"')
            if phases:
                self.send_header("Server-Timing", format_server_timing(phases))
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("ETag", f'"{etag}"')
        self.send_header("Content-Type", "application/json")
        if phases:
            self.send_header("Server-Timing", format_server_timing(phases))
            self.send_header("X-TQ-Timing", format_server_timing(phases))
        enc = self.headers.get("Accept-Encoding", "")
        out = body
        if "gzip" in enc.lower() and len(out) > 1024:
            import gzip
            out = gzip.compress(out, compresslevel=3)
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def _build_dashboard_payload(
        self,
        *,
        tab: str,
        qs: dict[str, list[str]],
        ver: Any,
        want_timing: bool,
    ) -> tuple[dict[str, Any], dict[str, float]]:
        phases: dict[str, float] = {}
        status = self._get_status(timings=phases)
        payload: dict[str, Any] = {"status": _slim_status_payload(status)}

        if tab in ("queue", "dashboard"):
            q = self._get_queue(timings=phases)
            payload["queue"] = {
                **{k: q[k] for k in ("total", "limit", "offset") if k in q},
                "tasks": [_queue_row_dict(t) for t in q.get("tasks", [])],
            }
            _t = time.perf_counter()
            payload["tags"] = self._get_tags(qs.get("project", [None])[0])
            phases["tags"] = round((time.perf_counter() - _t) * 1000.0)
            _t = time.perf_counter()
            payload["groups"] = self._get_groups()
            phases["groups"] = round((time.perf_counter() - _t) * 1000.0)
            _t = time.perf_counter()
            payload["lock_contention"] = self._lock_contention_cached(ver)
            phases["locks"] = round((time.perf_counter() - _t) * 1000.0)
            roots: dict[str, str] = {}
            for name, proj in self._projects_ref().items():
                roots[name] = str(proj.root) if hasattr(proj, "root") else ""
            payload["project_roots"] = roots
            op = (qs.get("started_by", [None])[0] or "").strip()
            if op:
                from taskqueue.queries import list_operator_projects
                payload["my_projects"] = list_operator_projects(self._rconn(), operator=op)
        elif tab == "history":
            try:
                offset = int(qs.get("offset", [0])[0])
            except (ValueError, IndexError):
                offset = 0
            try:
                limit = min(int(qs.get("limit", [100])[0]), 1000)
            except (ValueError, IndexError):
                limit = 100
            hist = _get_history_response(
                self._rconn(),
                project=qs.get("project", [None])[0],
                search=qs.get("search", [""])[0].strip(),
                offset=offset,
                limit=limit,
                hide_blocked=qs.get("hide_blocked", ["0"])[0] in ("1", "true"),
                started_by=(qs.get("started_by", [None])[0] or None),
            )
            payload["history"] = {
                **{k: hist[k] for k in ("total", "limit", "offset") if k in hist},
                "runs": [_history_row_dict(r) for r in hist.get("runs", [])],
            }
        elif tab == "workers":
            payload["workers"] = self._get_workers()

        if want_timing and phases:
            payload["_timing"] = {k: v for k, v in phases.items()}
        return payload, phases

    def _get_dashboard_route(self) -> None:
        """Single round-trip snapshot for the web UI with slim DTOs and ETag support."""
        qs = self._query()
        tab = qs.get("tab", ["queue"])[0]
        inm = self.headers.get("If-None-Match", "").strip().strip('"')
        want_timing = qs.get("timing", ["0"])[0] in ("1", "true")

        # ETag = process + DB change token + request shape. Live elapsed fields are not in the
        # validator — SSE patches those on the client so we can 304 until the DB actually changes.
        cheap_key: str | None = None
        ver: Any = None
        try:
            from taskqueue.web.change_tracker import tracker_for

            ver = tracker_for(self._db_path).token()
            if ver is not None:
                raw_key = f"{os.getpid()}|{ver}|{_dashboard_path_key(self.path)}"
                cheap_key = "v" + hashlib.sha256(raw_key.encode()).hexdigest()[:15]
        except Exception:
            cheap_key = None
        if cheap_key and inm == cheap_key:
            self._send_dashboard_bytes(etag=cheap_key, body=b"", phases=None, status=304)
            return

        cache_key = cheap_key or f"body|{_dashboard_path_key(self.path)}"
        leader = False
        wait_ev: threading.Event | None = None
        if not want_timing:
            with _dash_cache_lock:
                hit = _dash_cache.get(cache_key)
                now = time.monotonic()
                if hit is not None and now - hit[0] < _DASH_CACHE_TTL_SEC:
                    self._send_dashboard_bytes(
                        etag=hit[1], body=hit[2],
                        phases={**hit[3], "cached": 1},
                    )
                    return
                wait_ev = _dash_inflight.get(cache_key)
                if wait_ev is None:
                    leader = True
                    wait_ev = _dash_inflight[cache_key] = threading.Event()

            if not leader and wait_ev is not None:
                wait_ev.wait(timeout=30.0)
                with _dash_cache_lock:
                    hit = _dash_cache.get(cache_key)
                if hit is not None:
                    self._send_dashboard_bytes(
                        etag=hit[1], body=hit[2],
                        phases={**hit[3], "cached": 1},
                    )
                    return
                # Leader failed or timed out — fall through and build.

        etag = cheap_key or ""
        body = b"{}"
        phases: dict[str, float] = {}
        try:
            payload, phases = self._build_dashboard_payload(
                tab=tab, qs=qs, ver=ver, want_timing=want_timing,
            )
            _t_json = time.perf_counter()
            body_for_etag = json.dumps(payload, sort_keys=True, default=str).encode()
            phases["json"] = round((time.perf_counter() - _t_json) * 1000.0)
            etag = cheap_key or hashlib.sha256(body_for_etag).hexdigest()[:16]
            body = body_for_etag[:-1] + b',"etag":' + json.dumps(etag).encode() + b"}"

            if not want_timing:
                with _dash_cache_lock:
                    _dash_cache[cache_key] = (time.monotonic(), etag, body, dict(phases))
                    if len(_dash_cache) > 64:
                        oldest = min(_dash_cache.items(), key=lambda kv: kv[1][0])[0]
                        _dash_cache.pop(oldest, None)
        finally:
            if leader:
                with _dash_cache_lock:
                    done = _dash_inflight.pop(cache_key, None)
                if done is not None:
                    done.set()

        if inm and inm == etag:
            self._send_dashboard_bytes(etag=etag, body=b"", phases=phases, status=304)
            return
        self._send_dashboard_bytes(etag=etag, body=body, phases=phases)

    def _get_locks(self) -> dict[str, Any]:
        conn = self._rconn()
        return {
            "occupancy": lock_occupancy(conn),
            "contention": lock_contention_summary(conn),
        }

    def _get_queue(self, timings: dict[str, float] | None = None) -> dict[str, Any]:
        from taskqueue.health import read_ack

        qs = self._query()
        try:
            offset = max(0, int(qs.get("offset", [0])[0]))
        except (ValueError, IndexError):
            offset = 0
        try:
            limit = min(int(qs.get("limit", [200])[0]), 5000)
        except (ValueError, IndexError):
            limit = 200
        return _get_queue_response(
            self._rconn(),
            project=qs.get("project", [None])[0],
            status=qs.get("status", [None])[0],
            search=qs.get("search", [""])[0].strip(),
            worker=qs.get("worker", [None])[0],
            tag=qs.get("tag", [None])[0],
            group=qs.get("group", [None])[0],
            started_by=(qs.get("started_by", [None])[0] or None),
            hide_blocked=qs.get("hide_blocked", ["0"])[0] in ("1", "true"),
            new_failed=qs.get("new_failed", ["0"])[0] in ("1", "true"),
            ack_at=read_ack(Path(self._db_path).parent),
            sort=qs.get("sort", ["status"])[0],
            sort_dir=qs.get("sort_dir", ["asc"])[0],
            limit=limit,
            offset=offset,
            active_runs=self._active_runs,
            timings=timings,
        )

    def _get_report(self) -> dict[str, Any]:

        qs = self._query()
        project = qs.get("project", [None])[0]
        days_s = qs.get("days", ["7"])[0]
        try:
            days = max(1, min(int(days_s), 366))  # F-149
        except ValueError:
            days = 7
        return build_report(self._rconn(), project=project, days=days)

    def _get_plan(self) -> dict[str, Any]:
        from taskqueue.plan import build_plan

        qs = self._query()
        try:
            limit = min(max(int(qs.get("limit", ["20"])[0]), 1), 200)
        except ValueError:
            limit = 20
        return build_plan(self._rconn(), limit=limit)

    def _get_diagnose(self) -> dict[str, Any]:
        from taskqueue import config as tqconfig

        qs = self._query()
        project = qs.get("project", [None])[0]
        task_id = qs.get("task_id", [None])[0]
        if not project or not task_id:
            return {"error": "project and task_id required"}
        return build_diagnose(self._rconn(), project, task_id, home=tqconfig.state_home())

    def _get_history(self) -> dict[str, Any]:
        qs = self._query()
        project_filter = qs.get("project", [None])[0]
        search = qs.get("search", [""])[0].strip()
        try:
            offset = int(qs.get("offset", [0])[0])
        except (ValueError, IndexError):
            offset = 0
        try:
            limit = min(int(qs.get("limit", [100])[0]), 1000)
        except (ValueError, IndexError):
            limit = 100
        return _get_history_response(
            self._rconn(),
            project=project_filter,
            search=search,
            offset=offset,
            limit=limit,
            hide_blocked=qs.get("hide_blocked", ["0"])[0] in ("1", "true"),
            started_by=(qs.get("started_by", [None])[0] or None),
        )

    def _get_groups(self) -> dict[str, Any]:
        """Groups summary: task counts and status breakdown per group."""
        qs = self._query()
        project_filter = qs.get("project", [None])[0]
        from taskqueue.queries import tasks_cached

        conn = self._rconn()
        return tasks_cached(
            conn, "groups", project_filter, lambda: _get_groups_response(conn, project=project_filter)
        )

    def _get_suites(self) -> dict[str, Any]:
        """Suites summary: task counts and status breakdown per suite."""
        qs = self._query()
        project_filter = qs.get("project", [None])[0]
        return _get_suites_response(self._rconn(), project=project_filter)

    def _get_tags(self, project: str | None = None) -> dict[str, Any]:
        """Tags summary, memoised on the tasks fingerprint (full scan only when tasks change)."""
        from taskqueue.queries import tasks_cached

        return tasks_cached(self._rconn(), "tags", project, lambda: self._compute_tags(project))

    def _compute_tags(self, project: str | None = None) -> dict[str, Any]:
        """Return all unique tags across tasks, with task counts and per-tag project sets."""
        if project:
            rows = self._rconn().execute(
                "SELECT project, tags FROM tasks WHERE project = ? AND tags IS NOT NULL AND tags != '[]'",
                (project,),
            ).fetchall()
        else:
            rows = self._rconn().execute(
                "SELECT project, tags FROM tasks WHERE tags IS NOT NULL AND tags != '[]'"
            ).fetchall()
        counts: dict[str, int] = {}
        tag_projects: dict[str, set] = {}
        for row in rows:
            try:
                tags = json.loads(row["tags"] if hasattr(row, "keys") else row[1])
                proj = row["project"] if hasattr(row, "keys") else row[0]
                for t in (tags if isinstance(tags, list) else []):
                    counts[t] = counts.get(t, 0) + 1
                    tag_projects.setdefault(t, set()).add(proj)
            except Exception:
                pass
        return {
            "tags": sorted(counts.keys()),
            "counts": counts,
            "tag_projects": {t: sorted(ps) for t, ps in tag_projects.items()},
        }

    def _get_attention(self) -> dict[str, Any]:
        """Problems needing operator attention."""

        qs = self._query()
        project_filter = qs.get("project", [None])[0]
        started_by = qs.get("started_by", [None])[0]
        items = list_attention(
            self._rconn(), operator=started_by, project=project_filter
        )
        return {"items": items, "count": len(items)}

    def _get_cost(self) -> dict[str, Any]:
        """Return CPU-minute cost aggregated by project."""
        return _get_cost_response(self._rconn())

    def _get_notifications(self) -> dict[str, Any]:
        """W6.5: Return recent notify.jsonl events for in-UI bell."""
        from taskqueue.notify import iter_notify_events, notify_log_path

        qs = self._query()
        try:
            limit = max(1, min(int((qs.get("limit") or ["50"])[0]), 1000))
        except ValueError:
            limit = 50
        since = (qs.get("since") or [""])[0] or None
        kind = (qs.get("kind") or [""])[0]
        path = notify_log_path(self._db_path.parent)
        events: list[dict[str, Any]] = []
        if path.is_file():
            try:
                if kind == "alert":
                    # Alerts are sparse among task events: scan wider, then keep the newest N.
                    events = [
                        e for e in iter_notify_events(path, since=since, limit=max(limit * 20, 1000))
                        if str(e.get("status") or "").startswith("alert:")
                    ][-limit:]
                else:
                    events = iter_notify_events(path, since=since, limit=limit)
            except OSError:
                pass
        return {"events": events, "count": len(events)}

    def _get_usage(self) -> dict[str, Any]:
        """R10: Return CPU/GPU usage time series from usage_snapshot table."""
        qs = self._query()
        project_filter = qs.get("project", [None])[0]
        try:
            days = max(1, min(int(qs.get("days", ["7"])[0]), 366))  # F-149
        except ValueError:
            days = 7
        return _get_usage_response(self._rconn(), project=project_filter, days=days)

    def _serve_run_detail(self, run_id: int) -> None:
        """GET /api/runs/<id> — one run with spec snapshot and metrics."""
        from taskqueue.operator_features import run_detail

        d = run_detail(self._rconn(), run_id)
        if d is None:
            self._json({"ok": False, "error": "run not found"}, status=404)
            return
        self._json(d)

    def _serve_run_diff(self) -> None:
        """GET /api/runs/diff?a=&b= — differences between two runs."""
        from taskqueue.operator_features import diff_runs

        qs = self._query()
        try:
            a, b = int(qs.get("a", [""])[0]), int(qs.get("b", [""])[0])
        except ValueError:
            self._json({"ok": False, "error": "a and b must be run ids"}, status=400)
            return
        d = diff_runs(self._rconn(), a, b)
        if d is None:
            self._json({"ok": False, "error": "run not found"}, status=404)
            return
        self._json(d)

    def _serve_notify_dead(self) -> None:
        """GET /api/notify/dead — webhooks that exhausted their retries."""
        from taskqueue.notify import list_dead_letters

        qs = self._query()
        try:
            limit = max(1, min(int(qs.get("limit", ["100"])[0]), 1000))  # F-149
        except ValueError:
            limit = 100
        self._json({"dead": list_dead_letters(self._rconn(), limit=limit)})

    def _serve_metrics(self) -> None:
        """Prometheus text exposition; under /api so token and scope checks apply."""
        from taskqueue.metrics_export import render_metrics

        rss: float | None = None
        try:
            import os as _os

            import psutil as _psutil

            rss = round(_psutil.Process(_os.getpid()).memory_info().rss / 1_048_576, 1)
        except Exception:
            pass
        self._text(
            render_metrics(self._rconn(), daemon_rss_mb=rss),
            content_type="text/plain; version=0.0.4; charset=utf-8",
        )

    def _serve_deps(self) -> None:
        """Return dependency graph nodes and edges (optional project/focus/mode/suite)."""
        qs = self._query()
        project = qs.get("project", [None])[0]
        focus = qs.get("focus", [None])[0]
        mode = qs.get("mode", ["all"])[0] or "all"
        suite = qs.get("suite", [None])[0]
        fmt = (qs.get("format", ["json"])[0] or "json").lower()
        if fmt not in ("json", "dot"):
            self._json({"error": "format must be json or dot"}, status=400)
            return
        try:
            graph = _get_deps_response(
                self._rconn(), project=project, focus=focus, mode=mode, suite=suite,
            )
        except ValueError as exc:
            self._json({"error": str(exc)}, status=400)
            return
        if fmt == "dot":
            from taskqueue.web.responses import deps_to_dot

            self._text(deps_to_dot(graph))
        else:
            self._json(graph)

    def _get_tasks_filtered(self) -> dict[str, Any]:

        from taskqueue.queries import count_tasks, list_tasks

        qs = self._query()
        started_by = qs.get("started_by", [None])[0]
        project_filter = qs.get("project", [None])[0]
        # F-141: honor documented q/search; page in SQL (not post-slice of a 500 cap).
        search = (qs.get("q", qs.get("search", [""]))[0] or "").strip()
        try:
            offset = max(0, int(qs.get("offset", [0])[0]))
            limit = min(10000, max(1, int(qs.get("limit", [2000])[0])))
        except (ValueError, IndexError):
            offset, limit = 0, 2000
        conn = self._rconn()
        total = count_tasks(
            conn, started_by=started_by, project=project_filter, search=search
        )
        rows = list_tasks(
            conn,
            started_by=started_by,
            project=project_filter,
            search=search,
            limit=limit,
            offset=offset,
        )
        from taskqueue._db_tasks import run_counts
        counts = run_counts(conn, [(r["project"], r["task_id"]) for r in rows])
        tasks = [_task_dict(r, strip_secrets=True) for r in rows]
        for d in tasks:
            d["run_number"] = counts.get((d["project"], d["task_id"]), 0)
        return {"tasks": tasks, "total": total, "offset": offset, "limit": limit}

    def _serve_projects_config(self) -> None:
        projs = self._projects_ref()
        res = {}
        for name, p in projs.items():
            entry = {
                "root": str(p.root),
                "queue_dir": str(p.queue_dir),
                "git_url": p.git_url,
                "remote": bool(getattr(p, "remote", False)),
            }
            sync_push = getattr(p, "sync_push", ()) or ()
            if sync_push:
                entry["sync"] = {"push": list(sync_push)}
            res[name] = entry
        self._json(res)

    def _serve_logs(self, path: str) -> None:

        from taskqueue.db import get_run
        from taskqueue.logio import (
            DEFAULT_LOG_TAIL_BYTES,
            clamp_tail_bytes,
            format_log_tail,
            read_log_tail,
        )

        run_id_str = path[len("/api/logs/"):]
        try:
            run_id = int(run_id_str)
        except ValueError:
            self.send_error(400)
            return
        row = get_run(self._rconn(), run_id)
        if not row or not row["log_path"]:
            self._clear_stale_txn()
            row = get_run(self._rconn(), run_id)
        if not row or not row["log_path"]:
            self.send_error(404)
            return
        log_path = Path(row["log_path"])
        if not log_path.exists():
            self._text("(log file not yet created)")
            return
        qs = self._query()
        raw_from = qs.get("from", [None])[0]
        if raw_from and str(raw_from).isdigit():
            # Incremental fetch: only the bytes after a previous X-Log-Next offset.
            try:
                size = log_path.stat().st_size
                start = int(raw_from)
                if start <= size:
                    cap = 256 * 1024
                    with open(log_path, "rb") as f:
                        f.seek(start)
                        data = f.read(cap)
                    cut = max(data.rfind(b"\n"), data.rfind(b"\r"))
                    if cut != -1:
                        data = data[: cut + 1]
                    elif len(data) < cap:
                        data = b""  # a partial line: wait for its end
                    self._text(
                        data.decode("utf-8", errors="replace"),
                        extra_headers={
                            "X-Log-Size": str(size),
                            "X-Log-From": str(start),
                            "X-Log-Next": str(start + len(data)),
                        },
                    )
                    return
            except OSError as exc:
                self._text(f"(read error: {exc})")
                return
        raw_bytes = qs.get("bytes", [None])[0]
        max_bytes = clamp_tail_bytes(
            int(raw_bytes) if raw_bytes and str(raw_bytes).isdigit() else None,
            default=DEFAULT_LOG_TAIL_BYTES,
        )
        try:
            tail = read_log_tail(log_path, max_bytes=max_bytes)
        except OSError as exc:
            self._text(f"(read error: {exc})")
            return
        headers = {
            "X-Log-Size": str(tail.file_size),
            "X-Log-Truncated": "1" if tail.truncated else "0",
            "X-Log-Start-Offset": str(tail.start_offset),
            "X-Log-Next": str(tail.file_size),
        }
        self._text(format_log_tail(tail), extra_headers=headers)

    def _serve_cross_log_search(self) -> None:
        """GET /api/log-search?q=TEXT[&project=P][&runs=N][&limit=M] — case-insensitive search
        of the tails of the most recent runs' logs."""
        from taskqueue.logio import read_log_tail

        qs = self._query()
        query = (qs.get("q", [None])[0] or "").strip().lower()
        if not query:
            self._send_error_json(400, "q is required")
            return
        project = (qs.get("project", [None])[0] or "").strip()
        def _int(key: str, default: int, hi: int) -> int:
            raw = qs.get(key, [None])[0]
            return min(int(raw), hi) if raw and raw.isdigit() and int(raw) > 0 else default
        n_runs = _int("runs", 30, 200)
        limit = _int("limit", 100, 500)
        sql = "SELECT run_id, project, task_id, log_path FROM runs WHERE log_path IS NOT NULL AND log_path != ''"
        args: list = []
        if project:
            sql += " AND project = ?"
            args.append(project)
        sql += " ORDER BY run_id DESC LIMIT ?"
        args.append(n_runs)
        matches: list[dict] = []
        for r in self._rconn().execute(sql, args).fetchall():
            if len(matches) >= limit:
                break
            try:
                text = read_log_tail(Path(r["log_path"]), max_bytes=262144).text
            except (OSError, AttributeError):
                continue
            for line in text.splitlines():
                if query in line.lower():
                    matches.append({"run_id": r["run_id"], "project": r["project"],
                                    "task_id": r["task_id"], "line": line[:500]})
                    if len(matches) >= limit:
                        break
        self._json({"q": query, "matches": matches, "truncated": len(matches) >= limit})

    def _serve_log_search(self, project: str, task_id: str) -> None:
        """W6.13: GET /api/tasks/<project>/<task_id>/log-search?q=PATTERN&offset=N&max_bytes=N"""

        qs = self._query()
        query = (qs.get("q", [None])[0] or "").strip()
        raw_offset = qs.get("offset", [None])[0]
        raw_max = qs.get("max_bytes", [None])[0]
        max_bytes = min(int(raw_max) if raw_max and raw_max.isdigit() else 262144, 8 * 1024 * 1024)

        row = self._rconn().execute(
            "SELECT log_path FROM runs WHERE project = ? AND task_id = ? ORDER BY run_id DESC LIMIT 1",
            (project, task_id),
        ).fetchone()
        if not row or not row["log_path"]:
            self._json({"error": "no log found", "matches": [], "total_bytes": 0})
            return
        log_path = Path(row["log_path"])
        if not log_path.exists():
            self._json({"error": "log file missing", "matches": [], "total_bytes": 0})
            return

        file_size = log_path.stat().st_size
        offset = max(0, int(raw_offset)) if raw_offset and raw_offset.isdigit() else 0
        matches: list[str] = []
        next_offset = offset
        with open(log_path, "rb") as fh:
            fh.seek(offset)
            if query:
                pat_lower = query.lower()
                # Stream line-by-line within the byte window.
                while fh.tell() - offset < max_bytes:
                    raw = fh.readline()
                    if not raw:
                        break
                    line = raw.decode("utf-8", errors="replace")
                    if pat_lower in line.lower():
                        matches.append(line.rstrip("\n"))
                next_offset = fh.tell()
            else:
                chunk = fh.read(max_bytes)
                next_offset = offset + len(chunk)
                matches = chunk.decode("utf-8", errors="replace").splitlines()

        self._json({
            "project": project,
            "task_id": task_id,
            "query": query,
            "matches": matches,
            "match_count": len(matches),
            "offset": offset,
            "next_offset": next_offset,
            "total_bytes": file_size,
            "log_path": log_path.name,
        })

    def _get_workers(self) -> list[dict[str, Any]]:
        db_key = str(getattr(self, "_db_path", "") or "")
        _cached_at, _cached = _workers_cache_by_db.get(db_key, (0.0, []))
        if _cached and time.monotonic() - _cached_at < _workers_cache_ttl:
            return _cached

        from taskqueue import __version__ as _mv
        from taskqueue.db import SCHEMA_VERSION as _ms
        from taskqueue._db_runs import worker_health
        cfg = get_config(self._rconn())
        heartbeat_sec = float(cfg.get("heartbeat_sec", 30))
        rows = self._rconn().execute("SELECT * FROM workers ORDER BY worker_id").fetchall()
        out = []
        for r in rows:
            d = {k: v for k, v in dict(r).items() if k not in _WORKER_SECRET_COLS}
            wv = d.get("version") or ""
            ws = d.get("schema_version")
            version_ok = True
            version_note = None
            if wv:
                try:
                    wt = tuple(int(x) for x in wv.split(".")[:3])
                    mt = tuple(int(x) for x in _mv.split(".")[:3])
                    if wt[0] < mt[0]:
                        version_ok = False
                        version_note = f"major behind master ({_mv})"
                    elif wt < mt:
                        version_note = f"minor behind master ({_mv})"
                except Exception:
                    pass
            if ws is not None and int(ws) < _ms:
                version_ok = False
                version_note = version_note or f"schema {ws} < master {_ms}"
            d["version_ok"] = version_ok
            d["version_note"] = version_note
            # F-074: age-based health distinct from stored status (stale "online")
            d["health"] = worker_health(
                d.get("last_seen"), heartbeat_sec=heartbeat_sec
            )
            out.append(d)
        _workers_cache_by_db[db_key] = (time.monotonic(), out)
        return out

    def _serve_run_history(self, run_id: int) -> None:
        """C3: GET /api/runs/<run_id>/history — resource usage samples for a run."""
        qs = self._query()
        since_s = qs.get("since", [None])[0]
        cols = "ts, cpu_pct, ram_gib, vram_gib, gpu_util_pct"
        try:
            if since_s:
                since = int(since_s)
                rows = self._rconn().execute(
                    f"SELECT {cols} FROM resource_history "
                    "WHERE run_id=? AND ts>? ORDER BY ts LIMIT 2000",
                    (run_id, since),
                ).fetchall()
            else:
                rows = self._rconn().execute(
                    f"SELECT {cols} FROM resource_history "
                    "WHERE run_id=? ORDER BY ts LIMIT 2000",
                    (run_id,),
                ).fetchall()
        except sqlite3.OperationalError as e:
            # Pre-migration DB without gpu_util_pct
            if "gpu_util_pct" not in str(e) and "no such column" not in str(e).lower():
                raise
            if since_s:
                since = int(since_s)
                rows = self._rconn().execute(
                    "SELECT ts, cpu_pct, ram_gib, vram_gib FROM resource_history "
                    "WHERE run_id=? AND ts>? ORDER BY ts LIMIT 2000",
                    (run_id, since),
                ).fetchall()
            else:
                rows = self._rconn().execute(
                    "SELECT ts, cpu_pct, ram_gib, vram_gib FROM resource_history "
                    "WHERE run_id=? ORDER BY ts LIMIT 2000",
                    (run_id,),
                ).fetchall()
        self._json({"samples": [dict(r) for r in rows]})

    def _get_task_detail(self, project: str, task_id: str) -> dict[str, Any]:
        """GET /api/tasks/<project>/<task_id> — task detail for the drawer (secrets stripped)."""
        from taskqueue._db_runs import get_last_run_for_task
        conn = self._rconn()
        row = get_task(conn, project, task_id)
        if not row:
            return {"error": "not found"}
        # F-117: never expose env / args_override / yaml_source on operator read APIs.
        task = _task_dict(row, strip_secrets=True)
        last_run = get_last_run_for_task(conn, project, task_id)
        if last_run:
            # Merge run-level fields that may be more current (peak RAM, last exit code, etc.)
            for key in ("peak_ram_gib", "peak_vram_gib", "started_at", "finished_at", "progress_pct"):
                if last_run[key] is not None:
                    task.setdefault(key, last_run[key])
        from taskqueue._db_tasks import run_summary
        task.update(run_summary(conn, project, task_id))
        return {"task": task}

    def _serve_task_runs(self, project: str, task_id: str) -> None:
        """GET /api/tasks/<project>/<task_id>/runs — per-task run history (no capability secrets)."""
        from taskqueue._db_runs import get_runs_for_task
        rows = get_runs_for_task(self._rconn(), project, task_id)
        # F-116: project via _history_row_dict — never serialize finalize_token / log_path.
        self._json({"runs": [_history_row_dict(r) for r in rows]})

    def _serve_task_yaml(self, project: str, task_id: str) -> None:
        """L1: GET /api/tasks/<project>/<task_id>/yaml — raw yaml_source for a task."""
        row = self._rconn().execute(
            "SELECT yaml_source FROM tasks WHERE project=? AND task_id=?",
            (project, task_id),
        ).fetchone()
        if not row or not row["yaml_source"]:
            self.send_error(404)
            return
        yaml_path = Path(row["yaml_source"])
        mtime_ns: int | None = None
        if yaml_path.is_file():
            try:
                text = yaml_path.read_text(encoding="utf-8")
                mtime_ns = yaml_path.stat().st_mtime_ns
            except OSError:
                text = row["yaml_source"]
        else:
            text = row["yaml_source"]
        body = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if mtime_ns is not None:
            self.send_header("X-File-Mtime-Ns", str(mtime_ns))
        self.end_headers()
        self.wfile.write(body)

    def _serve_fits(self) -> None:
        from .hardware import _get_hardware_data, gpu_fit_summary

        q = self._query()

        def num(k: str) -> float:
            try:
                return max(0.0, float((q.get(k) or ["0"])[0]))
            except ValueError:
                return 0.0

        self._json(gpu_fit_summary(_get_hardware_data(), num("vram_gib"), num("ram_gib")))

    def _qs_project(self) -> str | None:
        qs = self._query()
        project = (qs.get("project", [None])[0] or "").strip()
        return project or None

    def _get_agent_usage_route(self) -> None:
        from taskqueue.agent_stats import agent_usage

        qs = self._query()

        def q(name: str) -> str | None:
            return (qs.get(name, [None])[0] or "").strip() or None

        try:
            days = int(q("days") or 7)
            limit = int(q("limit") or 200)
            self._json(agent_usage(
                self._rconn(), days=days, by=q("by"), task_by=q("task_by"),
                project=q("project"), channel=q("channel"), operator=q("operator"), limit=limit,
            ))
        except ValueError as exc:
            self._json({"ok": False, "error": str(exc)}, status=400)

    def _get_task_stats_route(self) -> None:
        qs = self._query()
        task_id = (qs.get("task_id", [None])[0] or "").strip() or None
        self._json({
            "tasks": task_stats(
                self._rconn(),
                project=self._qs_project(),
                task_id=task_id,
            ),
        })

    def _get_timeline_route(self) -> None:
        qs = self._query()
        try:
            hours = int(qs.get("hours", [24])[0])
        except (TypeError, ValueError):
            hours = 24
        from taskqueue.timeline_forecast import cached_forecast

        self._json(cached_forecast(
            self._rconn(), hours=hours, project=self._qs_project(), limit=300,
        ))

    def _get_schedules_route(self) -> None:
        self._json({"schedules": list_schedules(self._rconn(), project=self._qs_project())})

    def _get_alerts_route(self) -> None:
        self._json(list_alerts(self._rconn(), hardware=self._system_info))

    def _get_system_alerts_route(self) -> None:
        self._json(list_system_alerts(self._rconn()))

    def _get_audit_route(self) -> None:
        qs = self._query()
        try:
            limit = max(1, min(int(qs.get("limit", [200])[0]), 5000))  # F-149
        except (TypeError, ValueError):
            limit = 200
        try:
            before = qs.get("before_id", [None])[0]
            before_id = int(before) if before else None
        except ValueError:
            self._json({"error": "before_id must be an integer"}, status=400)
            return
        fmt = (qs.get("format", ["json"])[0] or "json").lower()
        if fmt not in ("json", "csv", "jsonl"):
            self._json({"error": "format must be json, csv or jsonl"}, status=400)
            return

        def _q(name: str) -> str | None:
            return qs.get(name, [None])[0] or None

        events = list_audit(
            self._rconn(), limit=limit, actor=_q("actor"), action=_q("action"),
            project=_q("project"), since=_q("since"), until=_q("until"), before_id=before_id,
        )
        if fmt == "csv":
            from taskqueue.operator_features import audit_to_csv

            self._text(audit_to_csv(events))
        elif fmt == "jsonl":
            self._text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events))
        else:
            self._json({"events": events})

    def _get_tokens_route(self) -> None:
        self._json({"tokens": list_tokens(self._rconn())})
