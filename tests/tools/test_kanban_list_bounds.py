"""Bounded list contracts through registered handlers and real SQLite."""
import json

import pytest


@pytest.fixture
def board(monkeypatch, tmp_path):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(tmp_path))
    for name in ('HERMES_KANBAN_TASK', 'HERMES_KANBAN_DB', 'HERMES_KANBAN_BOARD'):
        monkeypatch.delenv(name, raising=False)
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    kb.init_db()
    with kbc.connect_closing() as conn:
        ids = [kb.create_task(conn, title='record ' + str(i), assignee='builder',
                              tenant='sample', body='large body ' * 1000)
               for i in range(237)]
        # Tied sort keys make the id tie-breaker observable, not incidental FIFO.
        conn.execute("UPDATE tasks SET status='done', created_at=100, priority=0")
        conn.commit()
        other = kb.create_task(conn, title='other tenant', assignee='reviewer', tenant='other')
        kb.block_task(conn, other, reason='needs review', kind='needs_input')
    return ids


def raw(args):
    from tools import kanban_tools
    from tools.registry import registry
    entry = registry.get_entry('kanban_list')
    assert entry is not None
    return entry.handler(args)


def call(args):
    return json.loads(raw(args))


def test_default_rows_are_legacy_shape_and_opt_in_is_explicit(board):
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    fields = {'block_kind', 'block_recurrences', 'latest_block'}
    plain = call({'status': 'done', 'limit': 167})
    rich = call({'status': 'done', 'limit': 167, 'include_block_fields': True})
    with kbc.connect_closing() as conn:
        tasks = kb.list_tasks(conn, status='done', limit=167)
        states = kb.result_states(conn, tasks)
        legacy = [{**kt._task_summary_dict(kb, conn, t), 'result_state': states[t.id]} for t in tasks]
    assert plain['tasks'] == legacy
    assert all(not fields.intersection(row) for row in plain['tasks'])
    assert all(fields.issubset(row) for row in rich['tasks'])
    assert [{k: v for k, v in row.items() if k not in fields} for row in rich['tasks']] == legacy
    # Same-row pre-append envelope: compare bytes, not a historical different board.
    old_envelope = dict(tasks=legacy, count=167, limit=167, truncated=True,
                        next_limit=200, promoted=plain['promoted'])
    default_bytes = len(raw({'status': 'done', 'limit': 167}).encode())
    legacy_bytes = len(json.dumps(old_envelope).encode())
    assert default_bytes <= legacy_bytes
    print(json.dumps({'evidence_class': 'development fixture, not live acceptance',
                      'rows': 167, 'default_bytes': default_bytes,
                      'pre_append_same_row_bytes': legacy_bytes,
                      'opt_in_bytes': len(raw({'status': 'done', 'limit': 167,
                                               'include_block_fields': True}).encode())}))


def test_offset_enumerates_past_max_and_count_matches(board):
    offset, ids = 0, []
    for _ in range(20):
        page = call({'status': 'done', 'limit': 23, 'offset': offset})
        assert page['truncated'] == page['has_more']
        ids.extend(row['id'] for row in page['tasks'])
        if not page['has_more']:
            assert page['next_offset'] is None
            break
        assert page['next_offset'] == offset + page['count']
        offset = page['next_offset']
    else:
        pytest.fail('pagination did not terminate')
    assert ids == sorted(board)
    assert len(ids) == len(set(ids))
    counts = call({'mode': 'count', 'status': 'done'})
    assert counts['count'] == counts['counts_by_status']['done'] == len(ids)
    assert call({'status': 'done', 'offset': len(ids)})['tasks'] == []


def test_count_is_sql_only_and_all_statuses_are_returned(board, monkeypatch):
    from hermes_cli import kanban_db as kb
    def forbidden(*args, **kwargs):
        pytest.fail('count materialized list rows or summaries')
    monkeypatch.setattr(kb, 'list_tasks', forbidden)
    monkeypatch.setattr(kb, 'result_states', forbidden)
    out = call({'mode': 'count', 'include_archived': True})
    assert 'tasks' not in out
    assert set(out['counts_by_status']) == set(kb.VALID_STATUSES)
    assert out['count'] == len(board) + 1 == sum(out['counts_by_status'].values())
    assert len(raw({'mode': 'count', 'include_archived': True}).encode()) < 500
    assert call({'mode': 'count', 'assignee': 'builder', 'tenant': 'sample'})['count'] == len(board)
    assert call({'mode': 'count', 'assignee': 'reviewer', 'tenant': 'sample'})['count'] == 0


@pytest.mark.parametrize('args', [
    {'offset': -1}, {'offset': True}, {'offset': '1'}, {'offset': 1.5},
    {'mode': 'unknown'}, {'mode': 'count', 'offset': 1},
    {'mode': 'count', 'limit': 2}, {'mode': 'count', 'include_block_fields': True},
])
def test_invalid_list_arguments_are_errors(board, args):
    assert call(args).get('error')


def test_archived_filter_matches_count(board):
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect_closing() as conn:
        conn.execute("UPDATE tasks SET status='archived' WHERE id=?", (board[0],))
        conn.commit()
    assert call({'mode': 'count', 'status': 'done'})['count'] == len(board) - 1
    assert call({'mode': 'count', 'status': 'archived'})['count'] == 1
    assert call({'mode': 'count'})['counts_by_status']['archived'] == 0
    assert call({'mode': 'count', 'include_archived': True})['counts_by_status']['archived'] == 1
