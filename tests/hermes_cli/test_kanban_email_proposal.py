"""Proposal contract through real shared inspection -> pure verdict calls."""
import copy
from dataclasses import replace
import builtins
import os
import socket
import time

import pytest
import yaml

from hermes_cli.kanban_email_card import (
    ACTION_FENCES, REQUIRED_PATHS, EmailCardEnvelope, EmailCardRefusal,
    inspect_email_card,
)
from hermes_cli.kanban_email_proposal import validate_proposal

OWN = 't_76543210'
DECISION = '''```decision
card_kind: proposal
revision: r
requires: needs_input
options:
  - id: A
    label: 'DECISION=A: prepare'
    effect: prepare
    comment: 'DECISION=A: prepare'
```
'''
PROPOSAL = dict(kind='proposal', item=dict(message_id='A'*80),
                action='draft_prepared', effect='Prepare a draft',
                reversibility='reversible_gui',
                fences=sorted(ACTION_FENCES['draft_prepared']),
                evidence=dict(reason='Preparation rationale', snapshot_hash='a'*64,
                              thread_hash='not_reviewed'),
                receipts=dict(operations=[dict(key='draft', status='pending')]),
                verification=dict(card_id='t_01234567'))


def inspect(data, *, decision=DECISION, prose=''):
    return inspect_email_card(prose + '\n```emailcard\n' +
                             yaml.safe_dump(data, sort_keys=False) + '```\n' + decision)


def verdict(data, **kwargs):
    return validate_proposal(inspect(data, **kwargs), own_card_id=OWN)


def mutate(data, path, value, missing=False):
    target = data
    parts = path.split('.')
    for part in parts[:-1]:
        target = target.setdefault(part, {})
    if missing:
        del target[parts[-1]]
    else:
        target[parts[-1]] = value


@pytest.mark.parametrize('path', REQUIRED_PATHS['proposal'])
@pytest.mark.parametrize('empty', ['omitted', None, '', '   ', [], {}])
def test_each_required_field_named_refusal(path, empty):
    data = copy.deepcopy(PROPOSAL)
    mutate(data, path, empty, missing=empty == 'omitted')
    parsed = inspect(data)
    result = validate_proposal(parsed, own_card_id=OWN)
    assert isinstance(result, EmailCardRefusal), (path, empty, result)
    assert result.path == path and path in result.reason
    assert result == validate_proposal(parsed, own_card_id=OWN)


def test_acceptance_identity_no_fill_and_purity(monkeypatch):
    parsed = inspect(PROPOSAL)
    assert isinstance(parsed, EmailCardEnvelope)
    original = parsed.emailcard.to_dict()
    def forbidden(*args, **kwargs):
        raise AssertionError('ambient/model/network access')
    with monkeypatch.context() as scope:
        for owner, name in [(builtins, 'open'), (os, 'getenv'), (socket, 'socket'),
                            (socket, 'create_connection'), (time, 'time'), (time, 'monotonic')]:
            scope.setattr(owner, name, forbidden)
        assert validate_proposal(parsed, own_card_id=OWN) is parsed
        assert validate_proposal(parsed, own_card_id=OWN) is parsed
    assert original == PROPOSAL == parsed.emailcard.to_dict()
    assert 'expires' not in parsed.decision.to_dict()
    assert parsed.emailcard.item.subject is None
    assert parsed.emailcard.verification.kind is None
    # Missing findings cannot be bypassed by clearing the cached tuple.
    changed = copy.deepcopy(PROPOSAL)
    del changed['effect']
    parsed = replace(inspect(changed), required_issues=())
    assert validate_proposal(parsed, own_card_id=OWN).path == 'effect'


@pytest.mark.parametrize('action', tuple(ACTION_FENCES))
def test_each_action_and_each_required_fence(action):
    data = copy.deepcopy(PROPOSAL)
    data['action'] = action
    data['fences'] = sorted(ACTION_FENCES[action])
    assert isinstance(verdict(data), EmailCardEnvelope)
    for fence in data['fences']:
        changed = copy.deepcopy(data)
        changed['fences'].remove(fence)
        result = verdict(changed)
        assert result.code == 'missing_action_fence'
        assert result.path == 'fences' and fence in result.reason


@pytest.mark.parametrize('key', ['body', 'bodyPreview', 'html', 'text', 'extraction',
                                 'content', 'content_base64', 'draft_body', 'document', 'attachment_content'])
@pytest.mark.parametrize('container', ['item', 'decision'])
def test_content_keys_refused_not_item_reference(key, container):
    data = copy.deepcopy(PROPOSAL)
    decision = DECISION
    if container == 'item':
        data['item'] = {key: 'private message'}
    else:
        decision = DECISION.replace('    comment:', f'    {key}: private message\n    comment:')
    result = verdict(data, decision=decision)
    assert result.code == 'forbidden_content_key'
    assert key in result.path and 'private message' not in result.reason


@pytest.mark.parametrize('prose,code', [
    ('A'*80, 'base64_content_run'),
    ('```text\n' + '!'*401 + '\n```', 'oversized_prose_fence'),
    ('> ' + '!'*198, 'overlong_quoted_line'),
])
def test_shared_content_findings_refused(prose, code):
    parsed = inspect(PROPOSAL, prose=prose)
    assert isinstance(parsed, EmailCardEnvelope)
    result = validate_proposal(parsed, own_card_id=OWN)
    assert result.code == code


@pytest.mark.parametrize('value', ['will verify later', OWN, '', None])
def test_verification_not_promise_self_or_empty(value):
    data = copy.deepcopy(PROPOSAL)
    data['verification']['card_id'] = value
    result = verdict(data)
    assert isinstance(result, EmailCardRefusal)
    assert result.path == 'verification.card_id'


@pytest.mark.parametrize('own', [None, '', 'promise', 't_ABCDEF12', 123])
def test_own_id_required_to_check_independence(own):
    result = validate_proposal(inspect(PROPOSAL), own_card_id=own)
    assert result.path == 'own_card_id'


def test_subject_explicit_policy_and_optional_metadata():
    data = copy.deepcopy(PROPOSAL)
    data['item'].update(subject='Bounded header', sender_domain='example.org',
                        sender_display='Person', folder='inbox', received_date='2026-10-07')
    parsed = inspect(data)
    assert validate_proposal(parsed, own_card_id=OWN).path == 'item.subject'
    assert validate_proposal(parsed, own_card_id=OWN, subject_permitted=True) is parsed
    assert validate_proposal(parsed, own_card_id=OWN, subject_permitted='true').path == 'subject_permitted'


@pytest.mark.parametrize('decision,code', [
    ('', 'missing_decision_block'),
    (DECISION.replace('proposal', 'record'), 'decision_kind_mismatch'),
    (DECISION.replace("    comment: 'DECISION=A: prepare'\n", ''), 'malformed_decision_block'),
    (DECISION.replace("comment: 'DECISION=A: prepare'", "comment: '   '"), 'malformed_decision_block'),
    (DECISION + DECISION, 'malformed_decision_block'),
])
def test_decision_required_and_valid(decision, code):
    result = verdict(PROPOSAL, decision=decision)
    assert result.code == code and 'decision' in result.reason
    if decision and ('comment' not in decision or "comment: '   '" in decision):
        assert 'comment' in result.reason


@pytest.mark.parametrize('path,value', [
    ('counts', {'judged': 1}), ('dispute_lane', 'record only'),
    ('action', 'send'), ('reversibility', 'undo'), ('fences', ['alien']),
    ('item.message_id', 'private message body'), ('evidence.snapshot_hash', 'unknown'),
    ('receipts.operations', [{'key': '', 'status': 'pending'}]),
])
def test_parser_shape_refusals_preserved(path, value):
    data = copy.deepcopy(PROPOSAL)
    mutate(data, path, value)
    parsed = inspect(data)
    assert isinstance(parsed, EmailCardRefusal)
    assert validate_proposal(parsed, own_card_id=OWN) is parsed


def test_other_kind_or_unparsed_input_not_accepted():
    parsed = inspect(PROPOSAL)
    record = replace(parsed, emailcard=replace(parsed.emailcard, kind='record'))
    assert validate_proposal(record, own_card_id=OWN).path == 'kind'
    assert validate_proposal('raw prose', own_card_id=OWN).code == 'wrong_type'
