

def test_force_import_keeps_pre_import_backup(tmp_path):
    import sqlite3
    from taskqueue.db import open_db
    from taskqueue.export_import import export_home, import_home

    src = tmp_path / "src"
    src.mkdir()
    open_db(src / "queue.db").close()
    arc = export_home(src, tmp_path / "a.tar.gz", with_logs=False)
    dst = tmp_path / "dst"
    dst.mkdir()
    c = open_db(dst / "queue.db")
    c.execute("CREATE TABLE marker(x)")
    c.commit()
    c.close()
    import_home(dst, arc, force=True)
    bak = dst / "queue.db.pre-import.bak"
    assert bak.is_file()
    b = sqlite3.connect(str(bak))
    assert b.execute("SELECT name FROM sqlite_master WHERE name='marker'").fetchone()
    b.close()


def test_config_set_validates_all_pairs_before_writing(tmp_path, capsys):
    from taskqueue._cli_admin import cmd_config_set
    from taskqueue.db import get_config, open_db

    home = tmp_path / "h"
    rc = cmd_config_set(home, ["scheduler_tick_sec=3", "no_such_key=1"])
    assert rc == 1
    conn = open_db(home / "queue.db")
    assert "scheduler_tick_sec" not in get_config(conn) or get_config(conn)["scheduler_tick_sec"] != 3
    conn.close()


def test_remove_project_offline_drops_resource_history(tmp_path):
    from taskqueue._cli_task import cmd_remove_project
    from taskqueue.db import open_db

    home = tmp_path / "h"
    home.mkdir()
    c = open_db(home / "queue.db")
    c.execute("INSERT INTO tasks(project, task_id, status, command, enqueued_at, updated_at) VALUES('p','t','done','x',1,1)")
    c.execute("INSERT INTO runs(project, task_id, status, started_at) VALUES('p','t','done',1)")
    rid = c.execute("SELECT run_id FROM runs").fetchone()[0]
    c.execute("INSERT INTO resource_history(run_id, ts, cpu_pct) VALUES(?,1,1.0)", (rid,))
    c.commit()
    c.close()
    assert cmd_remove_project(home, "p", force=True) == 0
    c = open_db(home / "queue.db")
    assert c.execute("SELECT COUNT(*) FROM resource_history").fetchone()[0] == 0
    c.close()


def test_import_refuses_when_pid_file_has_create_time(tmp_path):
    import os
    import psutil
    import pytest
    from taskqueue.export_import import import_home

    ct = psutil.Process(os.getpid()).create_time()
    (tmp_path / "daemon.pid").write_text(f"{os.getpid()}:{ct}")
    arc = tmp_path / "a.tar.gz"
    arc.write_bytes(b"x")
    with pytest.raises(RuntimeError, match="daemon is running"):
        import_home(tmp_path, arc, force=True)


def test_stop_offline_unknown_task_fails(tmp_path, capsys):
    from taskqueue._cli_task import cmd_stop
    from taskqueue.db import open_db

    home = tmp_path / "h"
    home.mkdir()
    open_db(home / "queue.db").close()
    assert cmd_stop(home, "nope/missing") == 1
    assert "not found" in capsys.readouterr().out.lower()
