"""Durable-output silence ladder and independent enforcement budgets."""
import json
import signal
import time

import pytest
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as d


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(d, "_poll_worker_exit", lambda *args: None)
    monkeypatch.setattr(d, "_worker_alive", lambda *args: False)
    conn = kbc.connect(tmp_path / "kanban.db")
    signals = []
    yield conn, signals
    conn.close()


def running(env, elapsed=2500, bound=0):
    conn, _ = env
    tid = kb.create_task(conn, title="worker", assignee="worker", max_runtime_seconds=bound)
    kb.claim_task(conn, tid)
    now = int(time.time())
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET worker_pid=999999, worker_started_at='fingerprint', started_at=? WHERE id=?",
                     (now-elapsed, tid))
        conn.execute("UPDATE task_runs SET started_at=?, profile='worker' WHERE id=(SELECT current_run_id FROM tasks WHERE id=?)",
                     (now-elapsed, tid))
    return tid


def tick(env, **kw):
    conn, signals = env
    return d.detect_silent_running(conn, nudge_seconds=kw.get("nudge", 1200),
        kill_seconds=kw.get("kill", 2400), signal_fn=lambda p,s: signals.append((p,s)))


def events(env, tid, kind):
    return [e for e in kb.list_events(env[0], tid) if e.kind == kind]


def output(env, tid, author="worker"):
    kb.add_comment(env[0], tid, author=author, body="partial")


def prior(env, tid, outcome, n=3):
    conn = env[0]
    with kb.write_txn(conn):
        for i in range(n):
            conn.execute("INSERT INTO task_runs(task_id,profile,status,outcome,started_at,ended_at,metadata) VALUES(?,?,?,?,?,?,?)",
                (tid,"worker",outcome,outcome,1,2,json.dumps({"productive_wall": True})))


def wall(env):
    return d.enforce_max_runtime(env[0], signal_fn=lambda p,s: env[1].append((p,s)))


def test_t1_nudge(env):
    tid = running(env, elapsed=1300)
    assert tick(env) == [(tid,"silence_nudged")]
    e = events(env,tid,"silence_nudged")[0]
    assert e.run_id and e.payload["run_id"] == e.run_id
    assert env[0].execute("SELECT author FROM task_comments WHERE task_id=?",(tid,)).fetchone()[0] == "dispatcher"
    assert env[1] == []


def test_t2_dedup(env):
    tid = running(env, elapsed=1300)
    tick(env)
    assert tick(env) == []
    assert len(events(env,tid,"silence_nudged")) == 1


def test_t3_kill(env):
    tid = running(env)
    tick(env)
    assert tick(env) == [(tid,"silence_killed")]
    assert env[1] == [(999999,signal.SIGTERM)]
    assert kb.get_task(env[0],tid).status == "ready"
    assert kb.get_task(env[0],tid).consecutive_failures == 0
    assert not events(env,tid,"gave_up")
    assert events(env,tid,"silence_killed")[0].payload["retry_status"] == "ready"
    assert env[0].execute("SELECT outcome FROM task_runs WHERE task_id=?",(tid,)).fetchone()[0] == "silence_killed"


def test_t4_one_stage(env):
    tid = running(env)
    assert tick(env) == [(tid,"silence_nudged")]
    assert not env[1]


def test_t5_response(env):
    tid = running(env)
    tick(env)
    output(env,tid)
    assert tick(env) == [] and not env[1]


def test_t6_dispatcher_is_not_output(env):
    tid = running(env)
    tick(env)
    output(env,tid,"other-profile")
    assert tick(env) == [(tid,"silence_killed")]


def test_t7_heartbeat_is_not_output(env):
    tid = running(env)
    tick(env)
    with kb.write_txn(env[0]):
        for i in range(20):
            kb._append_event(env[0],tid,"heartbeat",{"note":"alive"})
    assert tick(env) == [(tid,"silence_killed")]


def test_t8_unbounded(env):
    tid = running(env)
    assert kb.get_task(env[0],tid).max_runtime_seconds is None
    tick(env)
    assert tick(env) == [(tid,"silence_killed")]


def test_t9_productive_wall(env):
    tid = running(env,elapsed=3700,bound=3600)
    output(env,tid)
    output(env,tid)
    assert wall(env) == [tid]
    assert kb.get_task(env[0],tid).consecutive_failures == 0
    assert kb.get_task(env[0],tid).status == "ready"
    assert not events(env,tid,"gave_up")
    payload = events(env,tid,"timed_out")[0].payload
    assert payload["productive_wall"] and payload["interim_comment_count"] == 2


def test_t10_silent_wall(env):
    tid = running(env,elapsed=3700,bound=3600)
    wall(env)
    assert kb.get_task(env[0],tid).consecutive_failures == 1


def assert_blocked(env,tid):
    task = kb.get_task(env[0],tid)
    assert task.status == "blocked" and task.block_kind == "breaker"
    assert events(env,tid,"gave_up")
    payload = events(env,tid,"blocked")[0].payload
    assert payload["kind"] == "breaker" and payload["reason"]
    kb.recompute_ready(env[0])
    assert kb.get_task(env[0],tid).status == "blocked"


def test_t11_productive_cap(env):
    tid = running(env,elapsed=3700,bound=3600)
    prior(env,tid,"timed_out")
    output(env,tid)
    wall(env)
    assert_blocked(env,tid)
    assert kb.get_task(env[0],tid).consecutive_failures == 0


def test_t12_breaker_observability(env):
    tid = running(env,elapsed=3700,bound=3600)
    with kb.write_txn(env[0]):
        env[0].execute("UPDATE tasks SET consecutive_failures=1 WHERE id=?",(tid,))
    wall(env)
    assert_blocked(env,tid)
    assert kb.unblock_task(env[0],tid)
    assert kb.get_task(env[0],tid).status == "ready"


def test_t13_silence_cap(env):
    tid = running(env)
    prior(env,tid,"silence_killed")
    tick(env)
    tick(env)
    assert_blocked(env,tid)
    assert kb.get_task(env[0],tid).consecutive_failures == 0


def test_t14_no_double_charge(env):
    tid = running(env,bound=3600)
    tick(env)
    # Actual reclaim order: wall first, then silence; silence closes the row.
    assert wall(env) == []
    tick(env)
    assert wall(env) == []
    assert not events(env,tid,"timed_out")
    assert kb.get_task(env[0],tid).consecutive_failures == 0
    tid2 = running(env,elapsed=3700,bound=3600)
    tick(env,kill=0)
    assert wall(env) == [tid2]
    assert tick(env) == []
    assert not events(env,tid2,"silence_killed")


def test_t15_disable(env):
    tid = running(env)
    assert tick(env,nudge=0) == []
    assert tick(env,kill=0) == [(tid,"silence_nudged")]
    assert tick(env,kill=0) == [] and not env[1]


def test_t16_creation_default(env):
    conn = env[0]
    tid = kb.create_task(conn,title="default")
    assert kb.get_task(conn,tid).max_runtime_seconds == 3600
    tid = kb.create_task(conn,title="explicit",max_runtime_seconds=17)
    assert kb.get_task(conn,tid).max_runtime_seconds == 17
    tid = kb.create_task(conn,title="off",max_runtime_seconds=0)
    assert kb.get_task(conn,tid).max_runtime_seconds is None


def test_t17_guidance():
    from agent.prompt_builder import KANBAN_GUIDANCE
    assert "max_runtime_seconds" in KANBAN_GUIDANCE


def test_t18_foreign_host(env):
    tid = running(env)
    with kb.write_txn(env[0]):
        env[0].execute("UPDATE tasks SET claim_lock='foreign-host:123' WHERE id=?",(tid,))
    assert tick(env) == [] and not env[1]


def test_t19_unverified_live(env,monkeypatch):
    tid = running(env)
    tick(env)
    with kb.write_txn(env[0]):
        env[0].execute("UPDATE tasks SET worker_started_at=? WHERE id=?",(d.UNVERIFIED_WORKER_FINGERPRINT,tid))
    monkeypatch.setattr(kb,"_pid_alive",lambda p:True)
    assert tick(env) == [] and not env[1]
    assert kb.get_task(env[0],tid).status == "running"
