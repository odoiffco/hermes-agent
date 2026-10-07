"""Shared D1 boundary: real connections, existing fixtures, no live-board I/O."""
import builtins
import copy
from dataclasses import replace
import json
import os
from pathlib import Path
import socket
import sqlite3
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_email_card as ec
from hermes_cli.kanban_email_board import resolve_board_slug
from hermes_cli import kanban_email_proposal as proposal
from hermes_cli import kanban_email_record as record
from tests.hermes_cli.test_kanban_email_record import RECORD, body
from tests.hermes_cli.test_kanban_email_proposal import PROPOSAL, DECISION, OWN
from tests.hermes_cli.test_kanban_board_content_policy import fresh_home
from hermes_cli import kanban_db_connect as kbc


def authored(kind):
    return body(RECORD if kind == 'record' else PROPOSAL,
                decision='' if kind == 'record' else DECISION)


@pytest.fixture
def email_conn(fresh_home):
    kb.create_board('sis-email')
    with kbc.connect(board='sis-email') as conn:
        yield conn


@pytest.mark.parametrize('kind', ['record', 'proposal'])
def test_acceptance_none_verbatim_persistence(email_conn, kind):
    text = authored(kind)
    assert ec.validate_email_card_body(email_conn, text, task_id=OWN, board='wrong') is None
    tid = kb.create_task(email_conn, title='Fixture', body=text, workspace_kind='scratch')
    assert kb.get_task(email_conn, tid).body == text


@pytest.mark.parametrize('kind', ['record', 'proposal'])
def test_kind_independent_content_guard_before_dispatch(email_conn, monkeypatch, kind):
    def bypass(*args, **kwargs):
        raise AssertionError('kind validator reached before content gate')
    monkeypatch.setattr(record, 'validate_record', bypass)
    monkeypatch.setattr(proposal, 'validate_proposal', bypass)
    text = authored(kind) + '\n> ' + 'quoted words ' * 20
    with pytest.raises(ValueError, match='overlong_quoted_line'):
        ec.validate_email_card_body(email_conn, text, task_id=OWN)


@pytest.mark.parametrize('kind', ['record', 'proposal'])
@pytest.mark.parametrize('mode', ['replacement', 'mutation'])
def test_kind_independent_no_fill_guard(email_conn, monkeypatch, kind, mode):
    def fill(parsed, **kw):
        if mode == 'replacement':
            return replace(parsed, emailcard=replace(parsed.emailcard, effect='Synthesized'))
        object.__setattr__(parsed.emailcard, 'effect', 'Synthesized')
        return parsed
    monkeypatch.setattr(record if kind == 'record' else proposal,
                        'validate_' + kind, fill)
    with pytest.raises(ValueError, match='refuse_never_fill'):
        ec.validate_email_card_body(email_conn, authored(kind), task_id=OWN)


@pytest.mark.parametrize('kind', ['record', 'proposal'])
def test_no_fill_guard_cannot_accept_missing_authored_fields(email_conn, monkeypatch, kind):
    data = copy.deepcopy(RECORD if kind == 'record' else PROPOSAL)
    del data['effect']
    monkeypatch.setattr(record if kind == 'record' else proposal,
                        'validate_' + kind, lambda parsed, **kw: parsed)
    with pytest.raises(ValueError, match='refuse_never_fill'):
        ec.validate_email_card_body(email_conn, body(data, decision='' if kind == 'record' else DECISION), task_id=OWN)


@pytest.mark.parametrize('kind', ['record', 'proposal'])
def test_field_refusal_identity_passthrough(email_conn, monkeypatch, kind):
    refusal = ec.EmailCardRefusal('sentinel_field', 'verification.card_id: sentinel', 'verification.card_id')
    monkeypatch.setattr(record if kind == 'record' else proposal,
                        'validate_' + kind, lambda *args, **kw: refusal)
    assert ec._email_card_verdict(authored(kind), task_id=OWN, subject_permitted=False) is refusal
    with pytest.raises(ValueError, match='^sentinel_field: verification.card_id: sentinel$'):
        ec.validate_email_card_body(email_conn, authored(kind), task_id=OWN)


@pytest.mark.parametrize('kind_value,code', [('alien', 'unknown_card_kind'), (None, 'missing_card_kind')])
def test_distinct_kind_refusal(email_conn, kind_value, code):
    data = copy.deepcopy(RECORD)
    if kind_value is None:
        del data['kind']
    else:
        data['kind'] = kind_value
    with pytest.raises(ValueError, match='^' + code + ':'):
        ec.validate_email_card_body(email_conn, body(data), task_id=OWN)


@pytest.mark.parametrize('replacement,code', [('card_kind: alien', 'unknown_card_kind'), ('', 'missing_card_kind')])
def test_decision_kind_distinct(email_conn, replacement, code):
    text = authored('proposal').replace('card_kind: proposal', replacement)
    with pytest.raises(ValueError, match='^' + code + ':'):
        ec.validate_email_card_body(email_conn, text, task_id=OWN)


@pytest.mark.parametrize('kind', ['record', 'proposal'])
def test_refusal_before_transaction_no_rows_or_events(email_conn, monkeypatch, kind):
    monkeypatch.setattr(kb, '_new_task_id', lambda: OWN)
    data = copy.deepcopy(RECORD if kind == 'record' else PROPOSAL)
    data['verification']['card_id'] = OWN
    text = body(data, decision='' if kind == 'record' else DECISION)
    before = email_conn.total_changes
    statements = []
    email_conn.set_trace_callback(statements.append)
    with pytest.raises(ValueError, match='self_verification'):
        kb.create_task(email_conn, title='Rejected', body=text, workspace_kind='scratch')
    email_conn.set_trace_callback(None)
    assert email_conn.total_changes == before
    assert not any(s.startswith(('INSERT', 'BEGIN', 'SAVEPOINT')) for s in statements)


def test_idempotency_return_does_not_validate_or_rewrite(email_conn):
    tid = kb.create_task(email_conn, title='Original', body='Plain', idempotency_key='once')
    assert kb.create_task(email_conn, title='Duplicate', body='```emailcard\nkind: alien\n```',
                          idempotency_key='once') == tid
    assert kb.get_task(email_conn, tid).body == 'Plain'


def test_subject_policy_real_reader(email_conn):
    data = copy.deepcopy(PROPOSAL)
    data['item']['subject'] = 'Bounded reference'
    text = body(data, decision=DECISION)
    with pytest.raises(ValueError, match='subject_not_permitted_by_board_policy'):
        ec.validate_email_card_body(email_conn, text, task_id=OWN)
    kb.write_board_metadata('sis-email', subject_permitted=True)
    assert ec.validate_email_card_body(email_conn, text, task_id=OWN, board='default') is None


def test_connection_not_ambient_board_intent(email_conn, monkeypatch):
    monkeypatch.setenv('HERMES_KANBAN_DB', str(kb.kanban_home() / 'kanban.db'))
    monkeypatch.setenv('HERMES_KANBAN_BOARD', 'default')
    with pytest.raises(ValueError, match='unknown_card_kind'):
        ec.validate_email_card_body(email_conn, '```emailcard\nkind: alien\n```', task_id=OWN, board='default')


def test_single_snapshot_per_call(email_conn, monkeypatch):
    from hermes_cli import kanban_email_board as adapter
    original = adapter.board_snapshot
    calls = []
    def count(conn):
        calls.append(conn)
        return original(conn)
    monkeypatch.setattr(adapter, 'board_snapshot', count)
    ec.validate_email_card_body(email_conn, authored('record'), task_id=OWN)
    assert calls == [email_conn]


def test_engagement_and_unresolvable_fail_closed(fresh_home):
    with kbc.connect() as conn:
        text = '```emailcard\nkind: alien\n```'
        assert ec.validate_email_card_body(conn, text, task_id=OWN, board='sis-email') is None
        with pytest.raises(ValueError, match='unknown_card_kind'):
            ec.validate_email_card_body(conn, text, task_id=OWN, opt_in=True)
    with sqlite3.connect(':memory:') as conn:
        assert ec.validate_email_card_body(conn, text, task_id=OWN) is None
        with pytest.raises(ValueError, match='board_unresolvable'):
            ec.validate_email_card_body(conn, text, task_id=OWN, opt_in=True)
        assert ec.validate_email_card_body(conn, 'Plain', task_id=OWN, opt_in=True) is None


@pytest.mark.parametrize('text', [None, '', 'Plain', '````example\n```emailcard\nkind: alien\n```\n````'])
def test_blockless_and_nested_machine_text_pass(email_conn, text):
    assert ec.validate_email_card_body(email_conn, text, task_id=OWN) is None


@pytest.mark.parametrize('text', ['```emailcard\nkind: record', '````emailcard\nkind: record\n````'])
def test_malformed_machine_fences_refused(email_conn, text):
    with pytest.raises(ValueError, match='truncated_block|bad_fence'):
        ec.validate_email_card_body(email_conn, text, task_id=OWN)


def test_pure_verdict_has_no_ambient_access(monkeypatch):
    text = authored('proposal')
    # Imports are warm before denying ambient operations; no model is involved.
    assert isinstance(ec._email_card_verdict(text, task_id=OWN, subject_permitted=False), ec.EmailCardEnvelope)
    def deny(*args, **kw):
        raise AssertionError('ambient I/O, environment, network or clock access')
    with monkeypatch.context() as m:
        for owner, name in [(builtins, 'open'), (os, 'getenv'), (socket, 'socket'),
                            (socket, 'create_connection'), (time, 'time'), (time, 'monotonic'),
                            (Path, 'open')]:
            m.setattr(owner, name, deny)
        first = ec._email_card_verdict(text, task_id=OWN, subject_permitted=False)
        second = ec._email_card_verdict(text, task_id=OWN, subject_permitted=False)
        assert first == second
        assert resolve_board_slug('/fixed/kanban/boards/sis-email/kanban.db', '/fixed') == 'sis-email'


def test_cli_and_tool_use_same_callable_real_refusal(email_conn, monkeypatch, capsys):
    import contextlib
    from hermes_cli import kanban as cli
    from tools import kanban_tools as tools
    from hermes_cli import kanban_db_connect as kbc
    # Each adapter uses the actual opened isolated-board connection; no validator mock.
    @contextlib.contextmanager
    def opened(*args, **kw):
        yield email_conn
    @contextlib.contextmanager
    def board(*args, **kw):
        yield kb, email_conn
    monkeypatch.setattr(kbc, 'connect_closing', opened)
    monkeypatch.setattr(tools, '_board', board)
    monkeypatch.setattr(tools, '_is_dispatcher_owned_worker', lambda: False)
    monkeypatch.setattr(tools, '_persisted_session_id', lambda *_: None)
    # Observe the same public callable while still running its real guard.
    original = ec.validate_email_card_body
    reached = []
    def observed(*args, **kw):
        reached.append(original)
        return original(*args, **kw)
    monkeypatch.setattr(ec, 'validate_email_card_body', observed)
    bad = '```emailcard\nkind: alien\n```'
    from hermes_cli.kanban_parser import build_parser
    import argparse
    root = argparse.ArgumentParser()
    build_parser(root.add_subparsers())
    args = root.parse_args(['kanban', '--board', 'sis-email', 'create', 'Fixture', '--assignee', 'rivet', '--body', bad])
    assert cli.kanban_command(args) == 1
    assert capsys.readouterr().err == 'kanban: unknown_card_kind: kind: record or proposal required\n'
    result = json.loads(tools._handle_create({'title': 'Fixture', 'assignee': 'rivet', 'body': bad}))
    assert result == {'error': 'kanban_create: unknown_card_kind: kind: record or proposal required'}
    assert reached == [original, original]
    assert cli.kb.create_task is kb.create_task
    assert email_conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0
