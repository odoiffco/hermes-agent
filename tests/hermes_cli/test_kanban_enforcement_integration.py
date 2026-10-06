"""Integration edges beyond T1–T19: config scope, attachments, identity and notifier."""
import json
import time
from types import SimpleNamespace

import pytest
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as d


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME",str(tmp_path))
    monkeypatch.setattr(kb,"_pid_alive",lambda p:False)
    monkeypatch.setattr(d,"_poll_worker_exit",lambda *a:None)
    monkeypatch.setattr(d,"_worker_alive",lambda *a:False)
    conn=kbc.connect(tmp_path/"kanban.db")
    yield conn
    conn.close()


def running(conn):
    tid=kb.create_task(conn,title="job",assignee="worker",max_runtime_seconds=0)
    kb.claim_task(conn,tid)
    now=int(time.time())
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET worker_pid=999999,worker_started_at='fingerprint' WHERE id=?",(tid,))
        conn.execute("UPDATE task_runs SET started_at=?,profile='worker' WHERE id=(SELECT current_run_id FROM tasks WHERE id=?)",(now-2500,tid))
    return tid


def tick(conn):
    return d.detect_silent_running(conn,nudge_seconds=1200,kill_seconds=2400,signal_fn=lambda *a:None)


def test_attachment_counts_only_in_run_window_and_owner(board):
    tid=running(board)
    now=int(time.time())
    with kb.write_txn(board):
        for author,created in [("worker",now-3000),("other",now),("worker",now+3600)]:
            board.execute("INSERT INTO task_attachments(task_id,filename,stored_path,uploaded_by,created_at) VALUES(?,?,?,?,?)",(tid,"x","/not-read",author,created))
    assert tick(board)==[(tid,"silence_nudged")]
    with kb.write_txn(board):
        board.execute("INSERT INTO task_attachments(task_id,filename,stored_path,uploaded_by,created_at) VALUES(?,?,?,?,?)",(tid,"x","/not-read","worker",now))
    assert tick(board)==[]
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET max_runtime_seconds=1 WHERE id=?",(tid,))
    d.enforce_max_runtime(board,signal_fn=lambda *a:None)
    event=[e for e in kb.list_events(board,tid) if e.kind=="timed_out"][0]
    assert event.payload["productive_wall"] and event.payload["interim_comment_count"]==1


def test_config_profile_a_b_a(tmp_path,monkeypatch):
    # Real readonly config resolution, no mocked policy reader.
    homes=[tmp_path/"a",tmp_path/"b"]
    for home,bound in zip(homes,[47,0]):
        home.mkdir()
        (home/"config.yaml").write_text(f"kanban:\n  default_max_runtime_seconds: {bound}\n  silence_nudge_seconds: 7\n  silence_kill_seconds: 0\n  productive_wall_limit: 1\n  silence_kill_limit: 2\n")
    for home,bound in [(homes[0],47),(homes[1],None),(homes[0],47)]:
        monkeypatch.setenv("HERMES_HOME",str(home))
        with kbc.connect(home/"kanban.db") as conn:
            tid=kb.create_task(conn,title="scoped")
            assert kb.get_task(conn,tid).max_runtime_seconds==bound
            cfg=d.enforcement_config()
            assert cfg["silence_nudge_seconds"]==7 and cfg["silence_kill_seconds"]==0
            assert cfg["productive_wall_limit"]==1 and cfg["silence_kill_limit"]==2


def test_recycled_pid_is_not_signalled(board,monkeypatch):
    tid=running(board)
    tick(board)
    calls=[]
    monkeypatch.setattr(kb,"_pid_alive",lambda p:True)
    monkeypatch.setattr(d,"_pid_recycled",lambda *a:True)
    d.detect_silent_running(board,nudge_seconds=1200,kill_seconds=2400,signal_fn=lambda *a:calls.append(a))
    assert calls==[] and kb.get_task(board,tid).status=="ready"


def test_nudge_is_per_run(board):
    tid=running(board)
    tick(board)
    tick(board)
    kb.claim_task(board,tid)
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET worker_pid=999999 WHERE id=?",(tid,))
        board.execute("UPDATE task_runs SET started_at=? WHERE id=(SELECT current_run_id FROM tasks WHERE id=?)",(int(time.time())-2500,tid))
    assert tick(board)==[(tid,"silence_nudged")]


def test_reclaim_wiring_uses_config_and_separates_results(board,tmp_path,monkeypatch):
    (tmp_path/"config.yaml").write_text("kanban:\n  silence_nudge_seconds: 1200\n  silence_kill_seconds: 2400\n")
    tid=running(board)
    for name in ("reap_worker_zombies","reap_terminal_workers","detect_stale_running","detect_crashed_workers"):
        monkeypatch.setattr(d,name,lambda *a,**kw:[])
    monkeypatch.setattr(kb,"release_stale_claims",lambda *a,**kw:0)
    monkeypatch.setattr(d,"_kill_fn",lambda fn:lambda *a:None)
    result=d.DispatchResult()
    d._run_reclaim_phase(board,result,stale_timeout_seconds=14400,failure_limit=2,reconcile_orphans=False)
    assert result.nudged==[tid] and result.silence_killed==[] and result.timed_out==[]
    result=d.DispatchResult()
    d._run_reclaim_phase(board,result,stale_timeout_seconds=14400,failure_limit=2,reconcile_orphans=False)
    assert result.silence_killed==[tid] and result.timed_out==[]


def test_breaker_notification_is_single_message():
    from gateway.kanban_watchers_notifier import _KanbanNotification
    # Exercise the real formatter; no transport required for pairing policy.
    gave=SimpleNamespace(kind="gave_up",created_at=1,payload={"trigger_outcome":"timed_out"})
    block=SimpleNamespace(kind="blocked",created_at=1,payload={"kind":"breaker","trigger_outcome":"timed_out"})
    n=object.__new__(_KanbanNotification)
    n.d={"events":[gave,block]}
    assert n.format_event(block) is None
