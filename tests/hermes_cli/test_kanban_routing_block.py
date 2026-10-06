"""Routing-state acceptance/falsifiers for t_389dc3d9 (fixture boards only)."""
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

QUEUE_SCRIPT = Path('/Users/mdoige/.hermes/scripts/kanban-operator-queue.py')

@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / '.hermes'))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path / 'kanban.db'))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    from hermes_cli import kanban_db as kb, kanban_db_dispatch as kbd
    # Fixture lanes, not this machine's roster/dispatch allowlist.
    monkeypatch.setattr(kbd, '_profile_exists_fn', lambda: lambda name: True)
    kb.init_db()
    return kb

def conn():
    from hermes_cli import kanban_db_connect as kbc
    return kbc.connect_closing()

def ready(kb, db, assignee='lumbergh'):
    tid = kb.create_task(db, title='fixture routing', assignee=assignee)
    with kb.write_txn(db):
        db.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    return tid

def tick(db, **kwargs):
    from hermes_cli import kanban_db_dispatch as kbd
    return kbd.dispatch_once(db, spawn_fn=lambda *a, **kw: 12345,
                             orchestrator_profile='lumbergh', **kwargs)

@pytest.mark.parametrize('assignee', ['lumbergh', None])
def test_T1_T2_router_never_executes(home, assignee):
    with conn() as db:
        tid = ready(home, db, assignee)
        res = tick(db, default_assignee='lumbergh')
        assert not any(s[0] == tid for s in res.spawned)
        assert tid in res.routing_blocked
        t = home.get_task(db, tid)
        assert (t.status, t.block_kind, t.assignee) == ('blocked', 'routing', 'lumbergh')
        if assignee is None:
            assert tid in res.auto_assigned_default
            ev = db.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='assigned'", (tid,)).fetchone()
            assert json.loads(ev[0])['source'] == 'kanban.default_assignee'

@pytest.mark.parametrize('control', [False, True])
def test_T3_queue_silent(home, tmp_path, control):
    with conn() as db:
        tid = ready(home, db)
        home.block_task(db, tid, kind='routing', reason='routing fixture')
        needs = None
        if control:
            needs = ready(home, db, 'rivet')
            home.block_task(db, needs, kind='needs_input', reason='pick one')
    env = dict(os.environ, OP_QUEUE_DB=str(home.kanban_db_path()), OP_QUEUE_STATE=str(tmp_path / 'queue.json'))
    proc = subprocess.run([sys.executable, str(QUEUE_SCRIPT)], env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert tid not in proc.stdout
    assert needs in proc.stdout if control else proc.stdout == ''

def test_T3b_suppression_predicate(home):
    from gateway.kanban_watchers_notifier import suppressed_event
    for kind in ('blocked', 'block_loop_detected'):
        assert suppressed_event(SimpleNamespace(kind=kind, payload={'kind':'routing'}))
        assert not suppressed_event(SimpleNamespace(kind=kind, payload={'kind':'needs_input'}))
    assert not suppressed_event(SimpleNamespace(kind='crashed', payload={}))

@pytest.mark.parametrize('lane,expected', [('rivet','ready'), ('lumbergh','blocked'), ('Lumbergh','blocked')])
def test_T4_release_only_to_executor(home, lane, expected):
    with conn() as db:
        tid = ready(home, db)
        home.block_task(db, tid, kind='routing', reason='fixture')
        assert home.reassign_task(db, tid, lane, reclaim_first=True, orchestrator_profile='lumbergh')
        t = home.get_task(db, tid)
        assert (t.status, t.assignee) == (expected, lane.lower())
        if lane == 'rivet':
            res = tick(db)
            assert any(s[:2] == (tid, lane) for s in res.spawned)

def test_T5_review_and_capability_unchanged(home):
    from hermes_cli import kanban_db_dispatch as kbd
    with conn() as db:
        tid = ready(home, db, 'rivet')
        assert home.request_review(db, tid, reviewer='pita')
        t = home.get_task(db, tid)
        assert (t.status, t.assignee) == ('review','pita')
        assert kbd.has_spawnable_review(db)
        cap = ready(home, db, 'rivet')
        home.block_task(db, cap, kind='capability', reason='wall')
        assert home.get_task(db, cap).block_kind == 'capability'
        res = tick(db)
        assert any(s[:2] == (tid, 'pita') for s in res.spawned)

def test_F3_routing_never_escalates(home):
    with conn() as db:
        tid = ready(home, db)
        for _ in range(home.BLOCK_RECURRENCE_LIMIT + 3):
            assert home.block_task(db, tid, kind='routing', reason='same cause')
            assert home.get_task(db, tid).status == 'blocked'
            assert db.execute('SELECT block_recurrences FROM tasks WHERE id=?',(tid,)).fetchone()[0] == 0
            assert home.unblock_task(db, tid)
        assert not db.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='block_loop_detected'",(tid,)).fetchone()

def test_dry_run_router_not_spawned_or_mutated(home):
    with conn() as db:
        tid = ready(home, db, None)
        before = list(db.execute('SELECT * FROM task_events'))
        res = tick(db, default_assignee='lumbergh', dry_run=True)
        assert tid in res.routing_blocked
        assert not res.spawned
        assert home.get_task(db, tid).status == 'ready'
        assert home.get_task(db, tid).assignee is None
        assert list(db.execute('SELECT * FROM task_events')) == before


@pytest.mark.parametrize('router,expected', [('lumbergh','ready'), ('','blocked')])
def test_reassign_reads_config_without_changing_other_blocks(home, router, expected):
    from hermes_constants import get_hermes_home
    root = get_hermes_home()
    root.mkdir(parents=True, exist_ok=True)
    (root / 'config.yaml').write_text('kanban:\n  orchestrator_profile: "' + router + '"\n')
    with conn() as db:
        tid = ready(home, db)
        assert home.block_task(db, tid, kind='routing', reason='fixture')
        assert home.reassign_task(db, tid, 'rivet', reclaim_first=True)
        assert home.get_task(db, tid).status == expected
        cap = ready(home, db, 'lumbergh')
        assert home.block_task(db, cap, kind='capability', reason='wall')
        assert home.reassign_task(db, cap, 'rivet', reclaim_first=True)
        assert home.get_task(db, cap).status == 'blocked'


def test_gateway_settings_and_cli_guard(home, monkeypatch, capsys):
    from gateway.kanban_watchers_dispatcher import _resolve_dispatcher_settings, _KanbanDispatcher
    from hermes_cli import kanban_ops
    import argparse
    cfg = {'orchestrator_profile':'lumbergh', 'default_assignee':'lumbergh'}
    settings = _resolve_dispatcher_settings(cfg, home)
    assert settings.orchestrator_profile == 'lumbergh'
    monkeypatch.setattr('hermes_cli.config.load_config', lambda: {'kanban':cfg})
    with conn() as db:
        tid = ready(home, db)
    assert kanban_ops._cmd_dispatch(argparse.Namespace(dry_run=True, json=True, max=None)) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt['routing_blocked'] == [tid]
    assert receipt['spawned'] == []
    res = _KanbanDispatcher(home, settings).tick_once_for_board('default')
    assert res.routing_blocked == [tid]
    assert res.spawned == []
