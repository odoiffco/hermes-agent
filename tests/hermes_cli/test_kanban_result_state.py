"""Outcome state is derived from the row and completed handoff, never status alone."""
from pathlib import Path
from types import SimpleNamespace
import json
import pytest
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = tmp_path / '.hermes'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    # HERMES_HOME isolates board I/O; leave Path.home intact for pytest's tmp_path cleanup.
    path = kb.kanban_db_path(board='default')
    kb._INITIALIZED_PATHS.discard(str(path.resolve()))
    kb.init_db()
    with kbc.connect() as c:
        yield c


def ready(conn):
    tid = kb.create_task(conn, title='result state', assignee='coder')
    assert kb.claim_task(conn, tid, claimer=kb._claimer_id()) is not None
    return tid


def test_null_with_summary(conn):
    tid = ready(conn)
    assert kb.complete_task(conn, tid, summary='shipped X, evidence at /path')
    assert kb.get_task(conn, tid).result is None
    assert kb.result_state(conn, kb.get_task(conn, tid)) == 'summary_only'


def test_explicit_verified_result(conn):
    tid = ready(conn)
    assert kb.complete_task(conn, tid, result='landed ref abc123')
    assert kb.result_state(conn, kb.get_task(conn, tid)) == 'verified_result'


@pytest.mark.parametrize('text', [
    'Completed without a stated result or evidence; verify deliverables.',
    'Review approved without additional evidence.',
])
def test_legacy_placeholder(conn, text):
    tid = ready(conn)
    assert kb.complete_task(conn, tid, summary='evidence exists')
    conn.execute('UPDATE tasks SET result = ? WHERE id = ?', (text, tid))
    conn.commit()
    assert kb.result_state(conn, kb.get_task(conn, tid)) == 'placeholder'


def test_true_no_outcome(conn):
    tid = ready(conn)
    assert kb.request_review(conn, tid, summary='request review')
    assert kb.complete_task(conn, tid)
    conn.execute('UPDATE task_runs SET summary = NULL WHERE task_id = ?', (tid,))
    conn.commit()
    assert kb.result_state(conn, kb.get_task(conn, tid)) == 'no_outcome_recorded'


def test_reader_surfaces(conn, monkeypatch):
    from hermes_cli import kanban as cli
    from tools import kanban_tools as tool
    from plugins.kanban.dashboard import plugin_api as api
    summary = ready(conn)
    assert kb.complete_task(conn, summary, summary='evidence in summary')
    verified = ready(conn)
    assert kb.complete_task(conn, verified, result='landed ref')
    placeholder = ready(conn)
    assert kb.complete_task(conn, placeholder, summary='legacy evidence')
    conn.execute('UPDATE tasks SET result = ? WHERE id = ?',
                 ('Completed without a stated result or evidence; verify deliverables.', placeholder))
    absent = ready(conn)
    assert kb.request_review(conn, absent, summary='review request')
    assert kb.complete_task(conn, absent)
    conn.execute('UPDATE task_runs SET summary = NULL WHERE task_id = ?', (absent,))
    conn.commit()
    cases = {summary: 'summary_only', verified: 'verified_result',
             placeholder: 'placeholder', absent: 'no_outcome_recorded'}
    for tid, expected in cases.items():
        text = cli.run_slash(f'show {tid}')
        assert any(line.strip().startswith('Outcome:') and line.split(':', 1)[1].strip() == expected
                   for line in text.splitlines())
        show = json.loads(cli.run_slash(f'show {tid} --json'))
        assert show['result_state'] == show['task']['result_state'] == expected
        assert json.loads(tool._handle_show({'task_id': tid}))['task']['result_state'] == expected
        assert api.get_task(tid, board=None, run_state_type=None, run_state_name=None)['task']['result_state'] == expected
        assert api._task_dict(kb.get_task(conn, tid), result_state=kb.result_state(conn, kb.get_task(conn, tid)))['result_state'] == expected
    assert '[legacy auto-placeholder — not a verified outcome]' in cli.run_slash(f'show {placeholder}')
    assert '⚠ no outcome recorded' in cli.run_slash(f'show {absent}')
    text_list = cli.run_slash('list')
    assert '[summary-only]' in text_list and '[NO OUTCOME]' in text_list and '[placeholder]' in text_list
    assert 'landed ref [NO OUTCOME]' not in text_list
    listed = {t['id']: t for t in json.loads(cli.run_slash('list --json'))}
    assert {tid: listed[tid]['result_state'] for tid in cases} == cases
    monkeypatch.setattr(tool, '_require_orchestrator_tool', lambda _: None)
    listed_tools = {t['id']: t for t in json.loads(tool._handle_list({'limit': 100}))['tasks']}
    assert {tid: listed_tools[tid]['result_state'] for tid in cases} == cases
    board = api.get_board(tenant=None, include_archived=False, board=None,
                          workflow_template_id=None, current_step_key=None)
    cards = {t['id']: t for col in board['columns'] if col['name'] == 'done' for t in col['tasks']}
    assert {tid: cards[tid]['result_state'] for tid in cases} == cases
    child = kb.create_task(conn, title='downstream', assignee='coder')
    kb.link_tasks(conn, parent_id=summary, child_id=child)
    kb.link_tasks(conn, parent_id=absent, child_id=child)
    context = kb.build_worker_context(conn, child)
    assert 'outcome state: summary_only' in context
    assert '(no outcome recorded — verify deliverables' in context
    assert api.get_task(child, board=None, run_state_type=None, run_state_name=None)['child_results'] == []
    parent = ready(conn)
    assert kb.complete_task(conn, parent, summary='parent')
    kb.link_tasks(conn, parent_id=parent, child_id=summary)
    assert api.get_task(parent, board=None, run_state_type=None, run_state_name=None)['child_results'][0]['result_state'] == 'summary_only'


def test_tool_list_uses_batch_state_lookup(conn, monkeypatch):
    from tools import kanban_tools as tool

    ids = [ready(conn) for _ in range(3)]
    for tid in ids:
        assert kb.complete_task(conn, tid, summary='off-row handoff')

    monkeypatch.setattr(tool, '_require_orchestrator_tool', lambda _: None)
    def forbid_per_task_lookup(*args):
        raise AssertionError('list must batch summary lookups, not query each task')
    monkeypatch.setattr(kb, 'latest_summary', forbid_per_task_lookup)
    tasks = {task['id']: task for task in json.loads(tool._handle_list({'limit': 100}))['tasks']}
    assert all(tasks[tid]['result_state'] == 'summary_only' for tid in ids)


def test_notification_and_event_contract(conn):
    from gateway.kanban_watchers_notifier import _fmt_completed
    from tui_gateway.session_notifications import _kb_completed
    empty_task = SimpleNamespace(result=None)
    placeholder_task = SimpleNamespace(result='Completed without a stated result or evidence; verify deliverables.')
    event = SimpleNamespace(payload={})
    notify = SimpleNamespace(head='Done', title='task', task=empty_task)
    assert '[no outcome recorded — verify deliverables]' in _fmt_completed(event, notify)[0]
    assert '[no outcome recorded — verify deliverables]' in _kb_completed(empty_task, {}, 'task')
    notify.task = placeholder_task
    assert '[legacy placeholder]' in _fmt_completed(event, notify)[0]
    assert '[legacy placeholder]' in _kb_completed(placeholder_task, {}, 'task')
    for result, summary, expected in [
        (None, 'evidence at /path', 'summary_only'),
        ('landed ref', None, 'verified_result'),
        ('Completed without a stated result or evidence; verify deliverables.', None, 'placeholder'),
    ]:
        tid = ready(conn)
        assert kb.complete_task(conn, tid, result=result, summary=summary)
        event = next(e for e in kb.list_events(conn, tid) if e.kind == 'completed')
        assert event.payload['result_state'] == expected
        assert 'result_len' in event.payload


def test_archived_export_round_trip(conn, tmp_path):
    from hermes_cli import kanban_transfer
    tid = ready(conn)
    legacy = 'Review approved without additional evidence.'
    assert kb.complete_task(conn, tid, result=legacy)
    conn.execute('UPDATE tasks SET status = ? WHERE id = ?', ('archived', tid))
    conn.commit()
    assert kb.result_state(conn, kb.get_task(conn, tid)) == 'placeholder'
    assert kb.result_states(conn, [kb.get_task(conn, tid)])[tid] == 'placeholder'
    archive = kanban_transfer.export_board('default', str(tmp_path / 'state-export'))['archive']
    imported = kanban_transfer.import_board(archive, 'state-import')
    with kbc.connect(board=imported['board']) as other:
        row = kb.get_task(other, tid)
        assert row.result == legacy
        assert kb.result_state(other, row) == 'placeholder'
        assert any(e.kind == 'completed' and e.payload['result_state'] == 'placeholder'
                   for e in kb.list_events(other, tid))


def test_nonterminal_state_is_null_in_json(conn):
    from hermes_cli import kanban as cli
    tid = kb.create_task(conn, title='not done', assignee='coder')
    assert json.loads(cli.run_slash(f'show {tid} --json'))['result_state'] is None


def test_nonterminal_and_archived(conn):
    tid = ready(conn)
    task = kb.get_task(conn, tid)
    for status in ('ready', 'running', 'blocked', 'review', 'archived'):
        conn.execute('UPDATE tasks SET status = ?, completed_at = NULL WHERE id = ?', (status, tid))
        conn.commit()
        assert kb.result_state(conn, kb.get_task(conn, tid)) is None
    conn.execute('UPDATE tasks SET completed_at = 123 WHERE id = ?', (tid,))
    conn.commit()
    assert kb.result_state(conn, kb.get_task(conn, tid)) == 'no_outcome_recorded'
