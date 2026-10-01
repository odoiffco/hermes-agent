"""Tenant fence: production NULL cards remain dispatchable, fixture cards need opt-in."""
from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_notify as kbn
from gateway.kanban_watchers_notifier import _Collector


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DISPATCH_TENANTS", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kbc.connect() as conn:
        yield conn


def test_dispatch_null_and_fenced_and_opted_in(board, monkeypatch, all_assignees_spawnable, caplog):
    production = kb.create_task(board, title="production", assignee="rivet")
    fixture = kb.create_task(board, title="fixture", assignee="rivet", tenant="fixture")
    assert kbd.has_spawnable_ready(board)
    with caplog.at_level("WARNING", logger="hermes_cli.kanban_db"):
        res = kbd.dispatch_once(board, dry_run=True)
    assert {t for t, _, _ in res.spawned} == {production}
    assert res.skipped_fenced_tenant == [(fixture, "fixture")]
    assert sum("tenant fence held" in r.message for r in caplog.records) == 1
    assert {row["id"] for row in kbd._lane_rows(board, "ready", frozenset())} == {production}
    monkeypatch.setenv("HERMES_KANBAN_DISPATCH_TENANTS", " other, fixture ")
    res = kbd.dispatch_once(board, dry_run=True)
    assert {t for t, _, _ in res.spawned} == {production, fixture}
    assert res.skipped_fenced_tenant == []


def test_fenced_only_is_not_spawnable(board, all_assignees_spawnable):
    kb.create_task(board, title="fixture", assignee="rivet", tenant="fixture")
    assert not kbd.has_spawnable_ready(board)


def test_real_claim_path_spawns_only_after_opt_in(board, monkeypatch, all_assignees_spawnable):
    tid = kb.create_task(board, title="fixture", assignee="rivet", tenant="fixture")
    calls = []
    spawn = lambda task, workspace, board=None: calls.append(task.id) or None
    res = kbd.dispatch_once(board, spawn_fn=spawn)
    assert calls == [] and res.skipped_fenced_tenant == [(tid, "fixture")]
    monkeypatch.setenv("HERMES_KANBAN_DISPATCH_TENANTS", "fixture")
    res = kbd.dispatch_once(board, spawn_fn=spawn)
    assert calls == [tid]
    assert [task_id for task_id, _, _ in res.spawned] == [tid]


def test_manual_claim_and_review_fenced(board, monkeypatch):
    tid = kb.create_task(board, title="fixture", assignee="rivet", tenant="fixture")
    assert kb.claim_task(board, tid) is None
    assert kb.get_task(board, tid).status == "ready"
    events = kb.list_events(board, tid)
    assert any(e.kind == "claim_rejected" and "tenant_fenced" in str(e.payload) for e in events), [(e.kind, e.payload) for e in events]
    board.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
    assert kb.claim_review_task(board, tid) is None
    assert kb.get_task(board, tid).status == "review"
    monkeypatch.setenv("HERMES_KANBAN_DISPATCH_TENANTS", "fixture")
    assert kb.claim_review_task(board, tid) is not None


def test_breach_guard(board, caplog):
    result = kbd.DispatchResult()
    with caplog.at_level("WARNING", logger="hermes_cli.kanban_db"):
        accepted = kbd._dispatch_lane_task(
            board, {"id": "t_x", "assignee": "rivet", "tenant": "fixture"}, "rivet", result,
            lane="ready", dry_run=True, ttl_seconds=None, board=None, failure_limit=2,
            spawn_fn=None, per_profile_cap=None, per_profile_running={},
        )
    assert not accepted
    assert result.skipped_fenced_tenant == [("t_x", "fixture")]
    assert any("TENANT FENCE BREACH" in r.message for r in caplog.records)


def test_notifier_keeps_cursor_for_fenced_subscription(board):
    tid = kb.create_task(board, title="fixture", assignee="rivet", tenant="fixture")
    kbn.add_notify_sub(board, task_id=tid, platform="telegram", chat_id="chat")
    sub = kbn.list_notify_subs(board, tid)[0]
    with kb.write_txn(board):
        kb._append_event(board, tid, "blocked", {"reason": "test"})
    assert max(e.id for e in kb.list_events(board, tid)) > sub["last_event_id"]
    batch = object.__new__(_Collector)
    assert batch._claim_for_sub(board, "default", sub) is None
    assert kbn.list_notify_subs(board, tid)[0]["last_event_id"] == sub["last_event_id"]


LIVE_BOARD = Path("/Users/mdoige/.hermes/kanban.db")
_NEUTRALIZE_SQL = """
DELETE FROM task_runs;
UPDATE tasks SET worker_pid=NULL, worker_started_at=NULL, last_heartbeat_at=NULL,
                 claim_lock=NULL, claim_expires=NULL;
DELETE FROM tasks WHERE status='running';
UPDATE tasks SET consecutive_failures=0, last_failure_error=NULL;
UPDATE tasks SET status='ready' WHERE id IN (SELECT id FROM tasks
  WHERE status='done' AND tenant IS NULL AND assignee='pita'
  ORDER BY created_at ASC LIMIT 2);
UPDATE tasks SET status='ready' WHERE id IN (SELECT id FROM tasks
  WHERE status='done' AND tenant IS NULL AND assignee='rivet'
  ORDER BY created_at ASC LIMIT 1);
"""


@pytest.mark.skipif(not LIVE_BOARD.exists(), reason="live board not on this host")
def test_preexisting_null_rows_on_live_copy_still_dispatch(all_assignees_spawnable, tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_DISPATCH_TENANTS", raising=False)
    copy = tmp_path / "live-copy.db"
    subprocess.run(["sqlite3", str(LIVE_BOARD), f".backup '{copy}'"], check=True)
    subprocess.run(["sqlite3", str(copy)], input=_NEUTRALIZE_SQL, text=True, check=True)
    conn = sqlite3.connect(copy)
    conn.row_factory = sqlite3.Row
    try:
        expected = {r["id"] for r in conn.execute(
            "SELECT id FROM tasks WHERE status IN ('ready','review') AND tenant IS NULL")}
        assert len(expected) >= 4, f"vacuous fatality check: only {expected}"
        spawn_calls = []
        res = kbd.dispatch_once(
            conn, dry_run=True, max_in_progress_per_profile=3,
            spawn_fn=lambda t, w, board=None: spawn_calls.append(t.id) or 1,
        )
        spawned_ids = {tid for (tid, _a, _w) in res.spawned}
        assert expected <= spawned_ids, f"FENCE FAILS CLOSED ON PRODUCTION: {expected - spawned_ids}"
        assert res.skipped_fenced_tenant == [], "live board has no non-NULL tenants"
        assert not spawn_calls
    finally:
        conn.close()
