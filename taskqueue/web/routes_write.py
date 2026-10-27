"""Handler mixin (extracted from make_handler)."""
from __future__ import annotations

# Broad shared namespace so method bodies keep working with minimal churn.
from .deps import (
    Path,
    UTC,
    _PROJECT_LIMIT_FIELDS,
    create_alert,
    create_token,
    datetime,
    delete_alert,
    enable_task,
    get_config,
    get_task,
    json,
    os,
    pause_task,
    request_stop_with_control,
    retry_skip_reason,
    retry_task,
    revoke_token,
    set_config,
    set_project_limit,
    set_task_priority,
    time,
    update_alert,
    update_system_alert,
    validate_config,
    write_audit,
)
import uuid

from taskqueue.fsutil import atomic_write_text
from typing import Any

from taskqueue._db_tasks import get_task_status
from taskqueue.dt_utils import iso_utc

class BulkOpError(Exception):
    """A bulk group/suite operation refused for safety (carries the HTTP status and JSON body)."""

    def __init__(self, status: int, payload: dict[str, Any]) -> None:
        super().__init__(payload.get("error", "bulk operation refused"))
        self.status = status
        self.payload = payload


def _qs_first(qs: dict | None, key: str) -> str | None:
    v = (qs or {}).get(key)
    if isinstance(v, (list, tuple)):
        v = v[0] if v else None
    return None if v is None else str(v)


def _qs_flag(qs: dict | None, key: str) -> bool:
    """True only for explicit truthy values (``?completed=0`` must NOT count as set)."""
    v = _qs_first(qs, key)
    return v is not None and v.strip().lower() in ("1", "true", "yes", "on")


class WriteRoutes:
    def _ownership_gate(self, proj: str, task_id: str, qs: dict) -> str | None:
        """Return an error string if this mutation should be blocked for
        cross-operator ownership, or None to proceed. `operator`/`force`
        come from the query string (?operator=NAME&force=1) -- this is
        self-reported like TQ_OPERATOR on the CLI, not real auth."""
        from taskqueue.ownership import ownership_from_qs
        return ownership_from_qs(self._wconn(), proj, task_id, qs)

    def _post_config(self) -> None:
        from taskqueue.config import VALID_CONFIG_KEYS

        data = self._read_json()
        if data is None:
            return
        errors = []
        parsed_items: list[tuple[str, Any]] = []
        for k, v in data.items():
            err = validate_config(k, v)
            if err:
                errors.append(err)
                continue
            expected_type = VALID_CONFIG_KEYS[k][0]
            parsed_items.append((k, expected_type(v)))
        if errors:
            # All-or-nothing: one invalid key rejects the whole update. Same error shape as other routes.
            self._json({"ok": False, "error": "; ".join(errors), "errors": errors}, status=400)
            return
        from taskqueue.config_audit import build_config_detail
        from taskqueue.db import get_config

        before = get_config(self._wconn()) if parsed_items else {}
        for k, v in parsed_items:
            set_config(self._wconn(), k, v)
        if parsed_items:
            self._audit("config", detail=build_config_detail(before, parsed_items))
        self._json({"ok": True})

    def _post_audit_rollback(self, audit_id_s: str) -> None:
        """POST /api/audit/<id>/rollback — restore the config values recorded before that change."""
        from taskqueue.config_audit import rollback_config_audit

        try:
            audit_id = int(audit_id_s)
        except ValueError:
            self._json({"ok": False, "error": "bad audit id"}, status=400)
            return
        status, body = rollback_config_audit(self._wconn(), audit_id)
        if status == 200 and body["restored"]:
            self._audit("config_rollback", target=str(audit_id), detail=",".join(body["restored"]))
        body.pop("changes", None)
        self._json(body, status=status)

    def _post_project_limits(self, proj_name: str) -> None:
        if not proj_name:
            self.send_error(400)
            return
        data = self._read_json()
        if data is None:
            return
        errors = []
        for field, val in data.items():
            if field not in _PROJECT_LIMIT_FIELDS:
                errors.append(f"Unknown field: {field!r}")
                continue
            try:
                v = float(val)
                if v < 0:
                    raise ValueError("negative")
            except Exception:
                errors.append(f"Invalid value for {field!r}: {val!r}")
        _mn, _mx = data.get("min_tasks"), data.get("max_tasks")
        if not errors and _mn is not None and _mx is not None and 0 < float(_mx) < float(_mn):
            errors.append("min_tasks cannot exceed max_tasks")
        if errors:
            self._json({"ok": False, "error": "; ".join(errors)}, status=400)
            return
        wdb = self._open_write_conn()
        try:
            wdb.execute("BEGIN IMMEDIATE")
            for field, val in data.items():
                if field in _PROJECT_LIMIT_FIELDS:
                    set_project_limit(wdb, proj_name, field, float(val), commit=False)
            wdb.commit()
        except Exception:
            try:
                wdb.rollback()
            except Exception:
                pass
            raise
        finally:
            wdb.close()
        self._audit(
            "project_limits",
            proj_name,
            proj_name,
            ",".join(sorted(k for k in data if k in _PROJECT_LIMIT_FIELDS)),
        )
        self._json({"ok": True})

    def _post_remove_task(self, path: str, qs: dict) -> None:
        """POST /api/remove/<project>/<task_id>[?operator=&force=1] — unregister a task (runs kept)."""
        from taskqueue.db import remove_task

        proj, task_id = self._split2(path[len("/api/remove/"):])
        if self._rconn().execute(
            "SELECT 1 FROM tasks WHERE project = ? AND task_id = ?", (proj, task_id)
        ).fetchone() is None:
            self._json({"ok": False, "error": f"task not found: {proj}/{task_id}"}, status=404)
            return
        own_err = self._ownership_gate(proj, task_id, qs)
        if own_err:
            self._json({"ok": False, "error": own_err}, status=403)
            return
        err = remove_task(self._wconn(), proj, task_id)
        if err is None:
            self._audit("remove", proj, task_id)
        self._json({"ok": err is None, "error": err}, status=200 if err is None else 409)

    def _post_remove_project(self, project: str, qs: dict) -> None:
        """POST /api/project/remove/<project>[?force=1] — delete a project's tasks and runs.

        Refuses while any of its tasks runs unless force=1 (does not touch projects.yaml)."""
        if not project:
            self._send_error_json(400, "missing project")
            return
        conn = self._wconn()
        running = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE project = ? AND status = 'running'", (project,)
        ).fetchone()[0]
        if running and (qs.get("force") or ["0"])[0] not in ("1", "true"):
            self._json({"ok": False, "error": f"{running} task(s) running in {project}; stop them or force"}, status=409)
            return
        n_tasks = conn.execute("DELETE FROM tasks WHERE project = ?", (project,)).rowcount
        n_runs = conn.execute("DELETE FROM runs WHERE project = ?", (project,)).rowcount
        conn.commit()
        self._audit("remove_project", project, project, f"{n_tasks} tasks, {n_runs} runs")
        self._json({"ok": True, "tasks": n_tasks, "runs": n_runs})

    def _post_priority(self, path: str, qs: dict) -> None:
        rest = path[len("/api/priority/"):]
        # Single parse: project/task_id/N
        try:
            proj, rem = self._split2(rest)
            task_id, sep, prio_s = rem.rpartition("/")
            if not sep or not task_id:
                raise ValueError("missing task_id")
            prio = int(prio_s)
            if not 0 <= prio <= 100:
                raise ValueError("priority out of range")
        except ValueError:
            self._send_error_json(400, "priority must be an integer 0-100 (path: project/task_id/N)")
            return
        own_err = self._ownership_gate(proj, task_id, qs)
        if own_err:
            self._json({"ok": False, "error": own_err}, status=403)
            return
        set_task_priority(self._wconn(), proj, task_id, prio)
        self._audit("priority", proj, task_id, str(prio))
        self._json({"ok": True})

    def _post_mark_processed(self, path: str) -> None:
        proj, task_id = self._split2(path[len("/api/mark-processed/"):])
        now = iso_utc(datetime.now(UTC))
        cur = self._wconn().execute(
            "UPDATE tasks SET processed_at = ? WHERE project = ? AND task_id = ?",
            (now, proj, task_id),
        )
        self._wconn().commit()
        if not (cur.rowcount or 0):
            self._json({"ok": False, "error": f"task not found: {proj}/{task_id}"}, status=404)
            return
        self._audit("mark_processed", proj, task_id)
        self._json({"ok": True})

    def _post_stop(self, path: str, qs: dict) -> None:
        proj, task_id = self._split2(path[len("/api/stop/"):])
        if self._rconn().execute(
            "SELECT 1 FROM tasks WHERE project = ? AND task_id = ?", (proj, task_id)
        ).fetchone() is None:
            self._json({"ok": False, "error": f"task not found: {proj}/{task_id}"}, status=404)
            return
        own_err = self._ownership_gate(proj, task_id, qs)
        if own_err:
            self._json({"ok": False, "error": own_err}, status=403)
            return
        reason = ((qs.get("reason") or [""])[0] or "").strip()[:200]
        if reason:
            # R9: operator-supplied reason on the latest run (shown in notifications and history).
            from taskqueue.db import set_run_stop_reason

            run_row = self._rconn().execute(
                "SELECT run_id FROM runs WHERE project=? AND task_id=? ORDER BY run_id DESC LIMIT 1",
                (proj, task_id),
            ).fetchone()
            if run_row:
                set_run_stop_reason(self._wconn(), run_row["run_id"], reason)
        self._json(self._stop_one(proj, task_id))

    def _stop_one(self, proj: str, task_id: str) -> dict[str, Any]:
        conn = self._wconn()
        # Short-circuit: queued tasks have no running process — mark interrupted
        # immediately instead of waiting for daemon pickup. The status guard is in the
        # UPDATE itself so a task the daemon claimed a moment ago is not overwritten.
        from taskqueue._time import _now

        now = _now()
        cur = conn.execute(
            "UPDATE tasks SET status = 'interrupted', updated_at = ?, finished_at = ? "
            "WHERE project = ? AND task_id = ? AND status = 'queued'",
            (now, now, proj, task_id),
        )
        conn.commit()
        if (cur.rowcount or 0) > 0:
            self._audit("stop", proj, task_id)
            return {"ok": True, "final_status": "interrupted"}
        request_stop_with_control(conn, Path(self._db_path).parent, proj, task_id, requested_by=self._requester())
        self._audit("stop", proj, task_id)
        return {"ok": True}

    def _post_sse_ticket(self) -> None:
        """POST /api/sse-ticket — mint a short-lived ticket for EventSource URLs (see sse_ticket)."""
        from taskqueue.auth import extract_request_token
        from taskqueue.sse_ticket import TICKET_TTL_SEC, issue_ticket

        token = extract_request_token(dict(self.headers))
        if not token:
            # Loopback/no-auth callers need no ticket: the stream is open to them already.
            self._json({"ticket": None, "ttl_sec": 0})
            return
        self._json({"ticket": issue_ticket(token), "ttl_sec": TICKET_TTL_SEC})

    def _post_run_rerun(self, run_id_s: str) -> None:
        """POST /api/runs/<id>/rerun — re-queue the run's task with that run's recorded args.

        The args snapshot replaces the task's argv tail for one run, so it is subject to the
        same ``overridable_args`` allowlist as ``args_override`` on the retry route.
        """
        from taskqueue.db import (
            check_arg_overrides, clear_args_override, retry_task, set_args_override,
        )

        try:
            run_id = int(run_id_s)
        except ValueError:
            self._json({"ok": False, "error": "invalid run id"}, status=400)
            return
        conn = self._wconn()
        run = conn.execute(
            "SELECT project, task_id, args_json FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if run is None:
            self._json({"ok": False, "error": "run not found"}, status=404)
            return
        try:
            args = json.loads(run["args_json"]) if run["args_json"] else None
        except ValueError:
            args = None
        if not isinstance(args, list):
            self._json({"ok": False, "error": "run has no recorded args"}, status=409)
            return
        project, task_id = run["project"], run["task_id"]
        own_err = self._ownership_gate(project, task_id, self._query())
        if own_err:
            self._json({"ok": False, "error": own_err}, status=403)
            return
        row = get_task(conn, project, task_id)
        if row is None:
            self._json({"ok": False, "error": "task not found"}, status=404)
            return
        args = [str(x) for x in args]
        err = check_arg_overrides(row, args)
        if err:
            self._json({"ok": False, "error": err}, status=409)
            return
        set_args_override(conn, project, task_id, args)
        if not retry_task(conn, project, task_id, home=Path(self._db_path).parent):
            clear_args_override(conn, project, task_id)
            self._json(
                {"ok": False, "error": f"cannot retry task with status '{row['status']}'"},
                status=409,
            )
            return
        self._audit("rerun", project, task_id, f"run={run_id}")
        self._json({"ok": True, "project": project, "task_id": task_id, "from_run": run_id})

    _BULK_ACTIONS = ("stop", "priority", "pause", "resume")
    _BULK_MAX = 5000

    def _post_tasks_bulk(self, qs: dict) -> None:
        """POST /api/tasks/bulk — one request instead of N serial POSTs.

        Body: ``{action: stop|priority|pause|resume, tasks: ["project/task_id", …],
        priority?: 0-100}``. Per-task failures are reported in ``results``; the call
        itself is 200 unless the body is malformed. Retry stays on ``/api/tasks/retry-batch``.
        """
        data = self._read_json()
        if data is None:
            return
        action = data.get("action")
        tasks = data.get("tasks")
        if action not in self._BULK_ACTIONS:
            self._json(
                {"ok": False, "error": f"action must be one of {list(self._BULK_ACTIONS)}"},
                status=400,
            )
            return
        if not isinstance(tasks, list) or not tasks or len(tasks) > self._BULK_MAX:
            self._json(
                {"ok": False, "error": f"tasks must be a list of 1-{self._BULK_MAX} 'project/task_id'"},
                status=400,
            )
            return
        prio = None
        if action == "priority":
            try:
                prio = int(data.get("priority"))
                if not 0 <= prio <= 100:
                    raise ValueError
            except (TypeError, ValueError):
                self._json({"ok": False, "error": "priority must be an integer 0-100"}, status=400)
                return
        keys: list[tuple[str, str]] = []
        for raw in tasks:
            s = str(raw)
            proj, sep, tid = s.partition("/")
            if not sep or not proj or not tid:
                self._json({"ok": False, "error": f"bad task id (need project/task): {s}"}, status=400)
                return
            keys.append((proj, tid))
        results: list[dict[str, Any]] = []
        for proj, tid in keys:
            item: dict[str, Any] = {"task": f"{proj}/{tid}"}
            try:
                own_err = self._ownership_gate(proj, tid, qs)
                if own_err:
                    item.update(ok=False, error=own_err)
                elif action == "stop":
                    item.update(self._stop_one(proj, tid))
                elif action == "priority":
                    set_task_priority(self._wconn(), proj, tid, prio)
                    self._audit("priority", proj, tid, str(prio))
                    item["ok"] = True
                else:
                    if action == "pause":
                        ok, err = pause_task(self._rconn(), proj, tid)
                    else:
                        ok, err = enable_task(self._rconn(), proj, tid)
                    if ok:
                        self._audit("enable", proj, tid, f"enabled={int(action == 'resume')}")
                    item["ok"] = bool(ok)
                    if err:
                        item["error"] = err
            except Exception as exc:  # one bad task must not abort the rest
                item.update(ok=False, error=str(exc))
            results.append(item)
        failed = sum(1 for r in results if not r.get("ok"))
        self._json({"ok": failed == 0, "action": action, "total": len(results),
                    "failed": failed, "results": results})

    # ------------------------------------------------------------------

    def _group_rows(
        self, conn: Any, grp_name: str, project: str | None, statuses: Any
    ) -> list[Any]:
        """Rows of a group (optionally project-scoped) in ``statuses``.

        The empty group name means "ungrouped" and is only meaningful per project: unscoped it
        matches EVERY project's ungrouped tasks (2026-10-02 incident: "Restart all" re-queued ~70
        completed tasks), so it is rejected without ``project``.
        """
        if grp_name == "":
            if not project:
                raise BulkOpError(
                    400,
                    {
                        "ok": False,
                        "error": "ungrouped bulk operations require ?project=<name> "
                        "(an empty group name matches every project)",
                    },
                )
            where = "(grp IS NULL OR grp = '') AND project = ?"
            params: list[Any] = [project]
        else:
            where = "grp = ?"
            params = [grp_name]
            if project:
                where += " AND project = ?"
                params.append(project)
        ordered = sorted(statuses)
        placeholders = ",".join("?" * len(ordered))
        return conn.execute(
            f"SELECT project, task_id, status FROM tasks WHERE {where} AND status IN ({placeholders})",
            (*params, *ordered),
        ).fetchall()

    def _group_stop(self, grp_name: str, qs: dict | None = None) -> dict[str, Any]:
        """Stop all running tasks in a group (``?project=`` scopes; required for ungrouped)."""
        project = _qs_first(qs, "project")
        conn = self._wconn()
        rows = self._group_rows(conn, grp_name, project, {"running"})
        if _qs_flag(qs, "dry_run"):
            return {"ok": True, "dry_run": True, "would_stop": len(rows)}
        stopped = 0
        for row in rows:
            try:
                request_stop_with_control(conn, Path(self._db_path).parent, row["project"], row["task_id"])
                stopped += 1
            except Exception:
                pass
        self._audit("group-stop", project or "", grp_name, f"n={stopped}")
        return {"ok": True, "stopped": stopped}

    def _group_retry(self, grp_name: str, qs: dict | None = None) -> dict[str, Any]:
        """Retry tasks in a group.

        * ``?project=`` scopes the group (required for the empty/ungrouped name).
        * Completed tasks are included only with an explicit ``?completed=1`` ("Restart all").
        * Restart-all must be confirmed with ``?confirm=<N>`` where N is the exact number of tasks
          that would be re-queued (409 + ``would_retry`` otherwise); ``?dry_run=1`` only counts.
        """
        from taskqueue._db_tasks import RETRYABLE_STATUSES

        project = _qs_first(qs, "project")
        include_completed = _qs_flag(qs, "completed")
        statuses = RETRYABLE_STATUSES if include_completed else RETRYABLE_STATUSES - {"completed"}
        conn = self._wconn()
        rows = self._group_rows(conn, grp_name, project, statuses)
        n_completed = sum(1 for r in rows if r["status"] == "completed")
        if _qs_flag(qs, "dry_run"):
            return {
                "ok": True,
                "dry_run": True,
                "would_retry": len(rows),
                "completed_included": n_completed,
            }
        if include_completed:
            confirm = _qs_first(qs, "confirm")
            if confirm is None or confirm != str(len(rows)):
                raise BulkOpError(
                    409,
                    {
                        "ok": False,
                        "error": "restart-all needs confirmation: repeat with confirm=<would_retry>",
                        "would_retry": len(rows),
                        "completed_included": n_completed,
                    },
                )
        retried = 0
        home = Path(self._db_path).parent
        for row in rows:
            if retry_task(conn, row["project"], row["task_id"], home=home,
                          force_enable=include_completed):
                retried += 1
        self._audit(
            "group-retry", project or "", grp_name,
            f"n={retried} completed={include_completed}",
        )
        return {"ok": True, "retried": retried}

    def _suite_retry(self, suite_name: str, qs: dict | None = None) -> dict[str, Any]:
        """Retry all retryable tasks in a named suite (incl. completed by design).

        An empty suite name would match every task without a suite, so it is rejected.
        """
        from taskqueue._db_tasks import RETRYABLE_STATUSES

        if not suite_name.strip():
            raise BulkOpError(400, {"ok": False, "error": "suite name required"})
        project = _qs_first(qs, "project")
        conn = self._wconn()
        ordered = sorted(RETRYABLE_STATUSES)
        placeholders = ",".join("?" * len(ordered))
        where = "suite = ?"
        params: list[Any] = [suite_name]
        if project:
            where += " AND project = ?"
            params.append(project)
        rows = conn.execute(
            f"SELECT project, task_id FROM tasks WHERE {where} AND status IN ({placeholders})",
            (*params, *ordered),
        ).fetchall()
        retried = 0
        home = Path(self._db_path).parent
        for row in rows:
            if retry_task(conn, row["project"], row["task_id"], home=home):
                retried += 1
        self._audit("suite-retry", project or "", suite_name, f"n={retried}")
        return {"ok": True, "retried": retried}

    def _task_retry(self, project: str, task_id: str) -> None:
        """F1: POST /api/tasks/<project>/<task_id>/retry — re-queue a retryable task.

        Optional JSON body: ``args_override`` (list), ``downstream`` (bool),
        ``enable_downstream`` (bool).
        """
        extra: dict[str, Any] = {}
        data = self._read_json()
        if data is None:
            return
        extra = data
        conn = self._wconn()
        if extra.get("enable_downstream"):
            from taskqueue.db import collect_downstream_task_ids
            from taskqueue.yaml_patch import patch_task_yaml
            from taskqueue.db import get_config, parse_task_yaml, upsert_task

            ids = [task_id] + collect_downstream_task_ids(conn, project, task_id)
            proj = self._projects_ref().get(project)
            enabled: list[str] = []
            if proj is not None:
                allow_remote = bool(get_config(conn).get("notify_allow_remote"))
                for tid in ids:
                    row = get_task(conn, project, tid)
                    if row is None or not row["yaml_source"]:
                        continue
                    path = Path(row["yaml_source"])
                    if not path.is_file():
                        continue
                    try:
                        patch_task_yaml(path, {"enabled": True}, task_id=tid)
                        upsert_task(
                            conn,
                            parse_task_yaml(path, proj, allow_remote_notify=allow_remote),
                        )
                        enabled.append(tid)
                    except Exception:
                        continue
            extra = {**extra, "_enabled": enabled}
        if extra.get("downstream") or extra.get("enable_downstream"):
            from taskqueue.db import retry_task_with_downstream

            result = retry_task_with_downstream(
                conn, project, task_id, home=Path(self._db_path).parent
            )
            requeued = result["requeued"]
            if not requeued:
                row = get_task_status(conn, project, task_id)
                if not row:
                    self._json({"ok": False, "error": "task not found"}, status=404)
                else:
                    self._json(
                        {
                            "ok": False,
                            "error": f"cannot retry task with status '{row['status']}' "
                            "(and no retryable downstream)",
                            "requeued": [],
                            "skipped": result["skipped"],
                            "enabled": extra.get("_enabled") or [],
                        },
                        status=409,
                    )
                return
            self._json(
                {
                    "ok": True,
                    "requeued": requeued,
                    "skipped": result["skipped"],
                    "enabled": extra.get("_enabled") or [],
                }
            )
            return
        # F-086: write override while still terminal, then retry (avoids claim race).
        override = extra.get("args_override")
        override_written = False
        if isinstance(override, list):
            from taskqueue.db import check_arg_overrides, set_args_override
            full_row = get_task(conn, project, task_id)
            if full_row is None:
                self._json({"ok": False, "error": "task not found"}, status=404)
                return
            err = check_arg_overrides(full_row, [str(x) for x in override])
            if err:
                self._json({"ok": False, "error": err}, status=409)
                return
            set_args_override(conn, project, task_id, [str(x) for x in override])
            override_written = True
        home = Path(self._db_path).parent
        from taskqueue._db_tasks import run_summary
        runs_before = run_summary(conn, project, task_id)["run_number"]
        ok = retry_task(conn, project, task_id, home=home)
        if not ok:
            skip = retry_skip_reason(conn, project, task_id)
            if skip == "disabled":
                # Explicit user retry re-enables the task in DB (YAML stays unchanged;
                # next sync may re-disable it after the task completes).
                conn.execute(
                    "UPDATE tasks SET enabled = 1 WHERE project = ? AND task_id = ?",
                    (project, task_id),
                )
                conn.commit()
                ok = retry_task(conn, project, task_id, home=home)
        if not ok:
            if override_written:
                from taskqueue.db import clear_args_override
                clear_args_override(conn, project, task_id)  # don't leak into a later run
            skip = retry_skip_reason(conn, project, task_id)
            row = get_task_status(conn, project, task_id)
            if not row:
                self._json({"ok": False, "error": "task not found"})
            elif skip == "max_attempts":
                self._json(
                    {"ok": False, "error": "max_attempts reached"},
                    status=409,
                )
            elif skip == "disabled":
                self._json(
                    {"ok": False, "error": "task is disabled"},
                    status=409,
                )
            else:
                self._json(
                    {"ok": False, "error": f"cannot retry task with status '{row['status']}'"},
                    status=409,
                )
            return
        self._json({"ok": True, "run_number_before": runs_before})

    def _task_retry_batch(self) -> None:
        """POST /api/tasks/retry-batch — batch re-queue in one SQLite transaction.

        Body: ``{project?, status?: str|list, task_ids?: ["p/t", …]}``.
        """
        from taskqueue.db import (
            RETRYABLE_STATUSES,
            get_all_tasks,
            retry_tasks_batch,
        )

        data = self._read_json()
        if data is None:
            return
        keys: list[tuple[str, str]] = []
        task_ids = data.get("task_ids") or []
        if isinstance(task_ids, list) and task_ids:
            for raw in task_ids:
                s = str(raw)
                if "/" not in s:
                    self._json(
                        {"ok": False, "error": f"bad task_id (need project/task): {s}"},
                        status=400,
                    )
                    return
                p, t = s.split("/", 1)
                keys.append((p, t))
        else:
            project = data.get("project")
            if not project:
                self._json(
                    {"ok": False, "error": "project or task_ids required"},
                    status=400,
                )
                return
            raw_status = data.get("status")
            if raw_status is None:
                # Completed tasks are re-run only on an explicit status filter (2026-10-02 incident).
                statuses = set(RETRYABLE_STATUSES) - {"completed"}
            elif isinstance(raw_status, list):
                statuses = {str(s) for s in raw_status}
            else:
                statuses = {s.strip() for s in str(raw_status).split(",") if s.strip()}
            bad = statuses - set(RETRYABLE_STATUSES)
            if bad:
                self._json(
                    {
                        "ok": False,
                        "error": f"invalid status filter: {sorted(bad)}",
                        "allowed": sorted(RETRYABLE_STATUSES),
                    },
                    status=400,
                )
                return
            conn = self._wconn()
            keys = [
                (row["project"], row["task_id"])
                for row in get_all_tasks(conn, project=str(project))
                if row["status"] in statuses
            ]
        result = retry_tasks_batch(self._wconn(), keys)
        if result["requeued"]:
            self._audit("retry-batch", "", "", f"n={len(result['requeued'])}")
        self._json({"ok": True, **result})

    def _task_patch(self, project: str, task_id: str) -> None:
        """P6: POST /api/tasks/<project>/<task_id>/patch — YAML field patch + upsert."""
        from taskqueue.yaml_patch import patch_task_yaml

        data = self._read_json()
        if data is None:
            return
        updates = data.get("updates") if isinstance(data.get("updates"), dict) else data
        if not isinstance(updates, dict) or not updates:
            self._json({"ok": False, "error": "updates dict required"}, status=400)
            return
        # strip wrapper keys if the whole body was sent
        updates = {k: v for k, v in updates.items() if k != "updates"}
        row = get_task(self._wconn(), project, task_id)
        if row is None or not row["yaml_source"]:
            self._json({"ok": False, "error": "task not found or has no yaml_source"}, status=404)
            return
        prior_status = str(row["status"] or "")
        path = Path(row["yaml_source"])
        try:
            applied = patch_task_yaml(path, updates, task_id=task_id)
        except ValueError as exc:
            self._json({"ok": False, "error": str(exc)}, status=400)
            return
        proj = self._projects_ref().get(project)
        if proj is None:
            self._json({"ok": False, "error": f"project {project} not registered"}, status=404)
            return
        from taskqueue.db import parse_task_yaml, upsert_task
        upsert_task(
            self._wconn(),
            parse_task_yaml(
                path, proj, allow_remote_notify=bool(get_config(self._wconn()).get("notify_allow_remote"))
            ),
        )
        payload: dict = {"ok": True, "applied": applied}
        from taskqueue._cli_helpers import _ENABLED_TERMINAL_STATUSES
        if updates.get("enabled") is True and prior_status in _ENABLED_TERMINAL_STATUSES:
            payload["warning"] = (
                f"{project}/{task_id} is enabled but status={prior_status} — "
                f"will not run until: tq retry {project}/{task_id}"
            )
        self._json(payload)

    def _cli_sync(self) -> None:
        """P6: POST /api/cli/sync — re-read operator_queue YAMLs (daemon is the writer).

        Body (all optional): ``{"projects": ["name", ...], "force": bool}``. Uses the daemon's
        mtime-cached ``_sync_project`` (unchanged files are not re-parsed, one commit per project);
        ``force`` re-parses everything.
        """
        from taskqueue.yaml_sync import _sync_project

        data = self._read_json()
        if data is None:
            return
        wanted = data.get("projects")
        if wanted is not None and (
            not isinstance(wanted, list) or not all(isinstance(x, str) for x in wanted)
        ):
            self._json({"ok": False, "error": "projects must be a list of names"}, status=400)
            return
        known = self._projects_ref()
        unknown = [n for n in (wanted or []) if n not in known]
        if unknown:
            self._json(
                {"ok": False, "error": f"Unknown project(s): {', '.join(unknown)}"}, status=404
            )
            return
        force = bool(data.get("force"))
        conn = self._wconn()
        total_upserted = 0
        total_deleted = 0
        per: dict[str, dict[str, int]] = {}
        for project in known.values():
            if wanted and project.name not in wanted:
                continue
            if not project.queue_dir.is_dir():
                continue
            stats = _sync_project(conn, project, force=force) or {}
            up = int(stats.get("upserted", 0))
            deleted = int(stats.get("deleted", 0))
            per[project.name] = {"upserted": up, "deleted": deleted}
            total_upserted += up
            total_deleted += deleted
        # F-119: report work done this call (not COUNT(*) of all tasks).
        # Keep ``total`` / flat project ints for older CLI parsers = upserted count.
        self._json({
            "ok": True,
            "total": total_upserted,
            "deleted": total_deleted,
            "projects": {name: v["upserted"] for name, v in per.items()},
            "projects_detail": per,
        })

    def _cli_archive(self) -> None:
        """POST /api/cli/archive — export old finished tasks to JSONL and drop them from the queue.

        Body (all optional): ``projects`` (names), ``older_than_days`` (default 30),
        ``statuses`` (default ["completed"]), ``dry_run`` (bool).
        """
        from taskqueue.archive import DEFAULT_STATUSES, archive_tasks

        data = self._read_json()
        if data is None:
            return
        wanted = data.get("projects")
        statuses = data.get("statuses") or list(DEFAULT_STATUSES)
        try:
            days = float(data.get("older_than_days", 30))
        except (TypeError, ValueError):
            days = -1.0
        if (
            days < 0
            or not isinstance(statuses, list)
            or not all(isinstance(x, str) for x in statuses)
            or (wanted is not None and (not isinstance(wanted, list) or not all(isinstance(x, str) for x in wanted)))
        ):
            self._json({"ok": False, "error": "invalid projects/statuses/older_than_days"}, status=400)
            return
        known = self._projects_ref()
        unknown = [n for n in (wanted or []) if n not in known]
        if unknown:
            self._json({"ok": False, "error": f"Unknown project(s): {', '.join(unknown)}"}, status=404)
            return
        result = archive_tasks(
            self._wconn(),
            known,
            home=Path(self._db_path).parent,
            older_than_days=days,
            statuses=tuple(statuses),
            only_projects=wanted or None,
            dry_run=bool(data.get("dry_run")),
        )
        if result["archived"] and not result["dry_run"]:
            self._audit("archive", detail=f"{result['archived']} task(s)")
        self._json({"ok": True, **result})

    def _cli_add(self) -> None:
        """P6: POST /api/cli/add — upsert YAML text for a registered project."""
        from taskqueue.db import get_config, parse_task_yaml_many, stamp_provenance, upsert_task

        data = self._read_json()
        if data is None:
            return
        project_name = str(data.get("project") or "")
        yaml_text = data.get("yaml_text")
        if not project_name or not isinstance(yaml_text, str) or not yaml_text.strip():
            self._json({"ok": False, "error": "project and yaml_text required"}, status=400)
            return
        if len(yaml_text.encode("utf-8")) > 1024 * 1024:
            self._json({"ok": False, "error": "YAML too large (max 1 MiB)"}, status=413)
            return
        proj = self._projects_ref().get(project_name)
        if proj is None:
            self._json({"ok": False, "error": f"Unknown project '{project_name}'"}, status=404)
            return
        explicit_name = str(data.get("filename") or "").strip()
        if explicit_name:
            fname = Path(explicit_name).name
            if not fname.endswith(".yaml") and not fname.endswith(".yml"):
                fname = fname + ".yaml"
            if ".." in fname or "/" in fname or "\\" in fname:
                self._json({"ok": False, "error": "invalid filename"}, status=400)
                return
        else:
            # Never reuse a fixed default: successive submissions would overwrite each other.
            fname = f"submitted_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}.yaml"
        dest = proj.queue_dir / fname
        dest.parent.mkdir(parents=True, exist_ok=True)
        allow_remote = bool(get_config(self._wconn()).get("notify_allow_remote"))
        # Validate from a scratch file first so invalid YAML never lands in the queue dir.
        tmp = dest.with_name(f".{fname}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            tmp.write_text(yaml_text, encoding="utf-8")
            parse_task_yaml_many(tmp, proj, allow_remote_notify=allow_remote)
            os.replace(tmp, dest)
            tasks = parse_task_yaml_many(dest, proj, allow_remote_notify=allow_remote)
        except Exception as exc:
            tmp.unlink(missing_ok=True)
            self._json({"ok": False, "error": str(exc)}, status=400)
            return
        ids = []
        for task in tasks:
            upsert_task(self._wconn(), task)
            ids.append(task.task_id)
        via = (self.headers.get("X-TQ-Via") or "").strip().lower() or "api"
        operator = (self.headers.get("X-TQ-Operator") or "").strip()
        client = (self.headers.get("X-TQ-Client") or self.headers.get("User-Agent") or "").strip()
        stamp_provenance(self._wconn(), project_name, ids, via, operator, client)
        self._audit("task_add", project_name, ",".join(ids)[:500], f"via={via} file={fname}")
        self._json({"ok": True, "task_ids": ids, "path": str(dest)})

    def _retry_dead_notify(self, dead_id: int) -> None:
        """POST /api/notify/dead/<id>/retry — replay one dead-lettered webhook."""
        from taskqueue.db import get_config
        from taskqueue.notify import retry_dead_letter

        conn = self._wconn()
        res = retry_dead_letter(conn, dead_id, allow_remote=bool(get_config(conn).get("notify_allow_remote")))
        self._json(res, status=int(res.pop("status", 200)))

    def _validate_yaml(self) -> None:
        """POST /api/validate — dry-run parse of YAML text; nothing is written to the queue."""
        import tempfile

        from taskqueue.db import get_config, parse_task_yaml_many

        data = self._read_json()
        if data is None:
            return
        project_name = str(data.get("project") or "")
        yaml_text = data.get("yaml_text")
        if not project_name or not isinstance(yaml_text, str) or not yaml_text.strip():
            self._json({"ok": False, "error": "project and yaml_text required"}, status=400)
            return
        if len(yaml_text.encode("utf-8")) > 1024 * 1024:
            self._json({"ok": False, "error": "YAML too large (max 1 MiB)"}, status=413)
            return
        proj = self._projects_ref().get(project_name)
        if proj is None:
            self._json({"ok": False, "error": f"Unknown project '{project_name}'"}, status=404)
            return
        allow_remote = bool(get_config(self._wconn()).get("notify_allow_remote"))
        with tempfile.TemporaryDirectory(prefix="tq_validate_") as td:
            tmp = Path(td) / "validate.yaml"
            tmp.write_text(yaml_text, encoding="utf-8")
            try:
                tasks = parse_task_yaml_many(tmp, proj, allow_remote_notify=allow_remote)
            except Exception as exc:
                self._json({"ok": False, "valid": False, "error": str(exc)})
                return
        self._json({"ok": True, "valid": True, "task_ids": [t.task_id for t in tasks]})

    def _task_set_priority(self, project: str, task_id: str) -> None:
        """N1: POST /api/tasks/<project>/<task_id>/priority — update task priority."""
        data = self._read_json()
        if data is None:
            return
        try:
            priority = int(data.get("priority", -1))
        except (TypeError, ValueError):
            self._json({"ok": False, "error": "priority must be an integer"}, status=409)
            return
        if not (0 <= priority <= 100):
            self._json({"ok": False, "error": "priority must be between 0 and 100"}, status=409)
            return
        own_err = self._ownership_gate(project, task_id, self._query())
        if own_err:
            self._json({"ok": False, "error": own_err}, status=403)
            return
        set_task_priority(self._wconn(), project, task_id, priority)
        self._audit("priority", project, task_id, str(priority))
        self._json({"ok": True, "priority": priority})

    def _task_save_yaml(self, project: str, task_id: str) -> None:
        """W3.5: PUT /api/tasks/<project>/<task_id>/yaml — overwrite the task's YAML file."""
        import yaml as _yaml
        # F-014: cap body at 1 MiB to prevent OOM / DoS
        _MAX_YAML = 1024 * 1024
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            length = -1
        if length < 0:
            self._json({"ok": False, "error": "invalid Content-Length"}, status=400)
            return
        if length > _MAX_YAML:
            self._json({"ok": False, "error": "YAML body too large (max 1 MiB)"}, status=413)
            return
        body_text = self.rfile.read(length).decode("utf-8")
        # F-014: validate YAML before writing — reject malformed or non-dict documents
        try:
            parsed = _yaml.safe_load(body_text)
        except _yaml.YAMLError as exc:
            self._json({"ok": False, "error": f"Invalid YAML: {exc}"}, status=400)
            return
        if not isinstance(parsed, dict):
            self._json({"ok": False, "error": "YAML must be a mapping (dict)"}, status=400)
            return
        task = get_task(self._wconn(), project, task_id)
        if not task or not task["yaml_source"]:
            self._json({"ok": False, "error": "Task not found or no yaml_source"}, status=404)
            return
        yaml_path = Path(task["yaml_source"])
        # W3.5: mtime check — refuse to clobber a file changed on disk since the GET
        if_mtime_raw = self.headers.get("X-If-Mtime-Ns")
        if if_mtime_raw and yaml_path.is_file():
            try:
                if_mtime = int(if_mtime_raw)
                actual_mtime = yaml_path.stat().st_mtime_ns
                if actual_mtime != if_mtime:
                    self._json(
                        {"ok": False, "error": "File changed on disk since you opened it — reload and retry"},
                        status=409,
                    )
                    return
            except (ValueError, OSError):
                pass
        try:
            atomic_write_text(yaml_path, body_text)
        except OSError as exc:
            self._json({"ok": False, "error": str(exc)}, status=500)
            return
        self._json({"ok": True})

    def _requester(self) -> str:
        """Who is asking, for logs: operator (if set) and client, e.g. `alice via cli (tq-cli/0.5)`."""
        op = (self.headers.get("X-TQ-Operator") or "").strip()
        via = (self.headers.get("X-TQ-Via") or "").strip() or "web"
        client = (self.headers.get("X-TQ-Client") or "").strip()
        who = f"{op} via {via}" if op else f"via {via}"
        return f"{who} ({client})" if client else who

    def _audit(self, action: str, project: str | None = None, target: str | None = None, detail: str | None = None) -> None:
        actor = (self.headers.get("X-TQ-Operator") or "").strip() or "web"
        write_audit(self._wconn(), actor, action, project, target, detail)

    def _post_health_ack(self) -> None:
        """Acknowledge current failures: health drops from red to amber until a new one."""
        from taskqueue.health import write_ack

        operator = self.headers.get("X-TQ-Operator") or "web"
        ack_at = write_ack(Path(self._db_path).parent, operator)
        self._audit("health_ack", None, None, ack_at)
        self._json({"ok": True, "ack_at": ack_at, "health": self._get_health()})

    def _post_alert(self) -> None:
        data = self._read_json()
        if data is None:
            return
        try:
            rule = create_alert(self._wconn(), data)
        except ValueError as exc:
            self._json({"ok": False, "error": str(exc)}, status=400)
            return
        self._audit("alert_create", rule.get("project"), str(rule.get("id")), rule.get("name"))
        self._json({"ok": True, "rule": rule})

    def _patch_alert(self, raw_id: str) -> None:
        try:
            rule_id = int(raw_id)
        except ValueError:
            self.send_error(400)
            return
        data = self._read_json()
        if data is None:
            return
        try:
            rule = update_alert(self._wconn(), rule_id, data)
        except ValueError as exc:
            self._json({"ok": False, "error": str(exc)}, status=400)
            return
        if rule is None:
            self._json({"ok": False, "error": "not found"}, status=404)
            return
        self._audit("alert_update", rule.get("project"), str(rule_id), rule.get("name"))
        self._json({"ok": True, "rule": rule})

    def _delete_alert(self, raw_id: str) -> None:
        try:
            rule_id = int(raw_id)
        except ValueError:
            self.send_error(400)
            return
        if not delete_alert(self._wconn(), rule_id):
            self._json({"ok": False, "error": "not found"}, status=404)
            return
        self._audit("alert_delete", target=str(rule_id))
        self._json({"ok": True})

    def _patch_system_alert(self, key: str) -> None:
        data = self._read_json()
        if data is None:
            return
        try:
            alert = update_system_alert(self._wconn(), key, data)
        except ValueError as exc:
            self._json({"ok": False, "error": str(exc)}, status=400)
            return
        if alert is None:
            self._json({"ok": False, "error": "not found"}, status=404)
            return
        self._audit(
            "system_alert_update",
            target=key,
            detail=f"enabled={alert['enabled']} windows_toast={alert['windows_toast']}",
        )
        self._json({"ok": True, "alert": alert})

    def _post_token(self) -> None:
        data = self._read_json()
        if data is None:
            return
        try:
            created = create_token(
                self._wconn(),
                str(data.get("name") or ""),
                str(data.get("scope") or ""),
                data.get("projects"),
            )
        except ValueError as exc:
            self._json({"ok": False, "error": str(exc)}, status=400)
            return
        self._audit(
            "token_create",
            target=str(created["id"]),
            detail=(
                f"name={created['name']} scope={created['scope']} prefix={created['prefix']}"
                + (f" projects={','.join(created['projects'])}" if created["projects"] else "")
            ),
        )
        self._json({"ok": True, **created})

    def _delete_token(self, raw_id: str) -> None:
        try:
            token_id = int(raw_id)
        except ValueError:
            self.send_error(400)
            return
        if not revoke_token(self._wconn(), token_id):
            self._json({"ok": False, "error": "not found"}, status=404)
            return
        self._audit("token_revoke", target=str(token_id))
        self._json({"ok": True})
