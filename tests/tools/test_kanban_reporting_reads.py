"""Reporting contracts through real tool dispatch and CLI rendering."""
import argparse
import json

import pytest


@pytest.fixture
def board(monkeypatch, tmp_path):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(tmp_path))
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    monkeypatch.delenv('HERMES_KANBAN_DB', raising=False)
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    kb.init_db()
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title='large record', body='opening ' * 20000, assignee='builder')
        old = kb.add_comment(conn, tid, author='builder', body='older decision')
        body = ('newest decision 😀\n' * 12000).strip()
        newest = kb.add_comment(conn, tid, author='reviewer', body=body)
        kb.block_task(conn, tid, reason='blocked reason ' * 200, kind='capability')
    return tid, old, newest, body


def dispatch(name, args):
    from tools import kanban_tools  # registers schemas/handlers
    from tools.registry import registry
    entry = registry.get_entry(name)
    assert entry is not None
    return json.loads(entry.handler(args))


def test_list_block_fields_append_and_reason_is_visible(board, capsys):
    from hermes_cli import kanban as cli, kanban_db_connect as kbc, kanban_db as kb
    from hermes_cli.kanban_output import _task_to_dict
    tid, *_ = board
    row = dispatch('kanban_list', {'status': 'blocked', 'include_block_fields': True})['tasks'][0]
    assert row['id'] == tid
    assert row['block_kind'] == 'capability'
    assert row['block_recurrences'] >= 1
    assert row['latest_block']['kind'] == 'capability'
    assert row['latest_block']['reason_truncated']
    assert row['latest_block']['reason_chars'] > len(row['latest_block']['reason'])
    args = argparse.Namespace(assignee=None, mine=False, status='blocked', tenant=None,
                              session=None, archived=False, workflow_template_id=None,
                              current_step_key=None, json=True)
    assert cli._cmd_list(args) == 0
    cli_row = json.loads(capsys.readouterr().out)[0]
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert task is not None
        legacy_keys = list(_task_to_dict(task, kb.result_state(conn, task)))
    assert list(cli_row) == legacy_keys + ['block_kind', 'block_recurrences', 'latest_block']
    assert cli_row['block_kind'] == row['block_kind']
    args.json = False
    assert cli._cmd_list(args) == 0
    assert 'block_kind=capability' in capsys.readouterr().out


def test_comments_newest_and_complete_chunk_reconstruction(board):
    tid, old, newest, body = board
    full = dispatch('kanban_show', {'task_id': tid})
    assert len(json.dumps(full)) > 130000
    args = {'task_id': tid, 'mode': 'comments'}
    chunks = []
    while True:
        out = dispatch('kanban_show', args)
        assert len(json.dumps(out)) < 30000
        assert out['comment']['id'] == newest
        assert out['truncated'] and out['omitted_comments'] == 1
        assert 'worker_context' in out['omitted_sections']
        assert 'comment_offset' in out['remainder_hint']
        chunks.append(out['comment']['body'])
        if out['next_comment_offset'] is None:
            break
        args.update(comment_id=newest, comment_offset=out['next_comment_offset'])
    assert ''.join(chunks) == body
    older = dispatch('kanban_show', {'task_id': tid, 'mode': 'comments', 'comment_id': old})
    assert older['comment']['body'] == 'older decision'
    assert older['next_comment_id'] == newest
    assert out['previous_comment_id'] == old


@pytest.mark.parametrize('extra', [
    {'comment_offset': -1}, {'comment_offset': True}, {'comment_offset': '0'},
    {'comment_offset': 9999999}, {'comment_id': 0}, {'comment_id': 9999999},
])
def test_bad_cursors_are_errors_not_silent_cuts(board, extra):
    assert dispatch('kanban_show', {'task_id': board[0], 'mode': 'comments', **extra}).get('error')


def test_comment_id_cannot_read_another_task(board):
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    with kbc.connect_closing() as conn:
        other = kb.create_task(conn, title='other', assignee='builder')
    assert dispatch('kanban_show', {'task_id': other, 'mode': 'comments', 'comment_id': board[2]}).get('error')
    empty = dispatch('kanban_show', {'task_id': other, 'mode': 'comments'})
    assert empty['comment'] is None and empty['comment_count'] == 0
    assert empty['truncated'] and empty['remainder_hint']


def test_cli_comments_uses_same_bounded_renderer(board, capsys):
    from hermes_cli import kanban as cli
    args = argparse.Namespace(task_id=board[0], mode='comments', comment_id=None,
                              comment_offset=0, json=True)
    assert cli._cmd_show(args) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == dispatch('kanban_show', {'task_id': board[0], 'mode': 'comments'})


def test_unblocked_rows_keep_history_distinct_from_status(board):
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    with kbc.connect_closing() as conn:
        kb.unblock_task(conn, board[0])
    row = dispatch('kanban_list', {'include_block_fields': True})['tasks'][0]
    assert row['status'] != 'blocked'
    assert row['latest_block']['kind'] == 'capability'


@pytest.mark.parametrize('kind', ['needs_input', 'transient', 'routing', None, 'dependency'])
def test_each_block_kind_and_rekind_are_readable(board, kind):
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title='kind contract', assignee='builder')
        assert kb.block_task(conn, tid, kind=kind, reason='new cause')
    row = next(r for r in dispatch('kanban_list', {'status': 'blocked', 'include_block_fields': True})['tasks'] if r['id'] == tid)
    effective = 'needs_input' if kind == 'dependency' else kind
    assert 'block_kind' in row and row['block_kind'] == effective
    assert row['latest_block']['kind'] == effective
    assert row['latest_block']['requested_kind'] == ('dependency' if kind == 'dependency' else None)


@pytest.mark.parametrize('body', ['😀' * 10000, '\x01' * 10000])
def test_serialized_comments_remain_under_inline_budget(board, body):
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    from tools.registry import registry
    with kbc.connect_closing() as conn:
        kb.add_comment(conn, board[0], author='author' * 10000, body=body)
    entry = registry.get_entry('kanban_show')
    assert entry is not None
    raw = entry.handler({'task_id': board[0], 'mode': 'comments'})
    assert len(raw.encode('utf-8')) < 30000
    out = json.loads(raw)
    assert out['comment']['author_truncated']
    assert out['comment']['body_truncated']
    assert out['next_comment_offset'] == out['chunk_chars']
