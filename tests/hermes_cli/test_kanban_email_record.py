"""Record branch acceptance on real typed parser results, not fabricated data."""
import copy
from dataclasses import replace
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from hermes_cli.kanban_email_card import inspect_email_card, EmailCardEnvelope, EmailCardRefusal
from hermes_cli.kanban_email_envelope import REQUIRED_PATHS, ACTING_COUNT_KEYS, ACTION_FENCES
from hermes_cli.kanban_email_record import validate_record

RECORD = dict(kind='record', action='no_action', effect='Pass recorded',
              fences=sorted(ACTION_FENCES['no_action']),
              evidence=dict(reason='Filer classification', thread_hash='not_reviewed'),
              receipts=dict(counters_after='01|-2|3|4|5|6|7|8|9'),
              verification=dict(card_id='t_01234567'), dispute_lane='Independent review',
              counts=dict(passes_in_window=1))
DECISION = '''```decision
card_kind: record
revision: authored-r
requires: needs_input
options:
  - id: A
    label: 'DECISION=A: dispute'
    effect: independent review
    comment: 'DECISION=A: dispute'
```
'''


def body(data, prose='', decision=''):
    return prose + '\n```emailcard\n' + yaml.safe_dump(data, sort_keys=False) + '```\n' + decision


def verdict(text, **kw):
    return validate_record(inspect_email_card(text), own_card_id=kw.get('own_card_id', 't_76543210'),
                           subject_permitted=kw.get('subject_permitted', False))


def mutate(path, value, omit=False):
    data = copy.deepcopy(RECORD)
    target = data
    parts = path.split('.')
    for part in parts[:-1]:
        target = target[part]
    if omit:
        del target[parts[-1]]
    else:
        target[parts[-1]] = value
    return data


@pytest.mark.parametrize('path', REQUIRED_PATHS['record'])
@pytest.mark.parametrize('empty', ['omit', '', [], {}, None, '   '])
def test_each_required_path_named_refusal(path, empty):
    result = verdict(body(mutate(path, empty, omit=empty == 'omit')))
    assert isinstance(result, EmailCardRefusal)
    assert result.code == 'record_missing_' + path
    assert result.path == path and path in result.reason
    assert result == verdict(body(mutate(path, empty, omit=empty == 'omit')))


def test_distinct_required_codes_and_identity_no_fill():
    codes = [verdict(body(mutate(p, None, omit=True))).code for p in REQUIRED_PATHS['record']]
    assert len(set(codes)) == len(codes)
    parsed = inspect_email_card(body(RECORD))
    before = parsed.emailcard.to_dict()
    accepted = validate_record(parsed, own_card_id='t_76543210', subject_permitted=False)
    assert accepted is parsed
    assert before == RECORD == accepted.emailcard.to_dict()
    assert accepted.decision is None
    assert verdict(body(RECORD)) == verdict(body(RECORD))


@pytest.mark.parametrize('key', sorted(ACTING_COUNT_KEYS))
@pytest.mark.parametrize('operations', ['omit', [], [dict(key='applied act', status='verified')]])
def test_conditional_acts_receipts(key, operations):
    data = copy.deepcopy(RECORD)
    data['counts'][key] = 1
    if operations != 'omit':
        data['receipts']['operations'] = operations
    result = verdict(body(data))
    if not operations or operations == 'omit':
        assert result.code == 'record_missing_receipts.operations'
        assert 'receipts.operations' in result.reason
    else:
        assert isinstance(result, EmailCardEnvelope)
        assert result.emailcard.to_dict() == data
    data['counts'][key] = 0
    assert isinstance(verdict(body(data)), EmailCardEnvelope)


@pytest.mark.parametrize('parent', ['evidence', 'receipts', 'verification'])
def test_missing_parent_and_cached_issue_cannot_hide_required_field(parent):
    data = copy.deepcopy(RECORD)
    del data[parent]
    parsed = inspect_email_card(body(data))
    # Even a caller discarding the cache cannot bypass no-fill.
    result = validate_record(replace(parsed, required_issues=()), own_card_id='t_76543210', subject_permitted=False)
    assert result.code.startswith('record_missing_' + parent + '.')


@pytest.mark.parametrize('name', ['body','bodyPreview','html','text','extraction','content',
                                  'content_base64','draft_body','document','attachment_content'])
@pytest.mark.parametrize('container', ['emailcard', 'decision'])
def test_known_content_keys_never_accepted(name, container):
    data = copy.deepcopy(RECORD)
    decision = ''
    if container == 'emailcard':
        data['unknown'] = {'nested': {name: 'private message'}}
    else:
        decision = DECISION.replace('    comment:', f'    {name}: private message\n    comment:')
    result = verdict(body(data, decision=decision))
    assert result.code == 'record_forbidden_content_key'
    assert name in result.path
    assert 'private message' not in result.reason


@pytest.mark.parametrize('prose,code', [('A'*80, 'base64_content_run'),
    ('```text\n'+'!'*401+'\n```', 'oversized_prose_fence'),
    ('> '+'!'*198, 'overlong_quoted_line')])
def test_typed_content_findings_enforced(prose, code):
    parsed = inspect_email_card(body(RECORD, prose))
    assert isinstance(parsed, EmailCardEnvelope)
    assert validate_record(parsed, own_card_id='t_76543210', subject_permitted=False).code == 'record_' + code


@pytest.mark.parametrize('action', sorted(ACTION_FENCES))
def test_all_action_fences(action):
    data = copy.deepcopy(RECORD)
    data['action'] = action
    data['fences'] = sorted(ACTION_FENCES[action])
    assert isinstance(verdict(body(data)), EmailCardEnvelope)
    for fence in ACTION_FENCES[action]:
        changed = copy.deepcopy(data)
        changed['fences'].remove(fence)
        result = verdict(body(changed))
        assert result.code == 'record_missing_action_fence'
        assert fence in result.reason


def test_policy_ids_kind_and_parser_failure():
    assert verdict(body(RECORD), own_card_id='t_01234567').code == 'record_self_verification'
    assert verdict(body(RECORD), own_card_id=None).code == 'record_invalid_own_card_id'
    assert verdict(body(RECORD), subject_permitted='true').code == 'record_invalid_subject_policy'
    data = copy.deepcopy(RECORD)
    data['item'] = dict(subject='', sender_domain='example.org', message_id='A'*20)
    assert verdict(body(data)).code == 'record_subject_not_permitted_by_board_policy'
    accepted = verdict(body(data), subject_permitted=True)
    assert accepted.emailcard.to_dict() == data
    assert verdict(body(mutate('kind', 'unknown'))).code == 'record_out_of_bounds'
    assert verdict(body(mutate('action', 'send'))).path == 'action'
    assert validate_record('not parsed', own_card_id='t_76543210', subject_permitted=False).code == 'record_invalid_input'


@pytest.mark.parametrize('expires', ['', '\nexpires: 2026-10-08T12:00:00Z'])
def test_optional_decision_expires_unchanged(expires):
    text = body(RECORD, decision=DECISION.replace('revision: authored-r', 'revision: authored-r' + expires))
    parsed = inspect_email_card(text)
    accepted = validate_record(parsed, own_card_id='t_76543210', subject_permitted=False)
    assert accepted is parsed
    if expires:
        assert accepted.decision.expires == '2026-10-08T12:00:00Z'
    else:
        assert 'expires' not in accepted.decision.to_dict()
    assert verdict(text) == verdict(text)


@pytest.mark.parametrize('decision', [DECISION.replace('card_kind: record', 'card_kind: proposal'),
    DECISION.replace("    comment: 'DECISION=A: dispute'\n", ''),
    DECISION.replace('revision: authored-r', 'revision: authored-r\nexpires:'),
    DECISION + DECISION])
def test_invalid_present_decision_never_optional(decision):
    result = verdict(body(RECORD, decision=decision))
    assert isinstance(result, EmailCardRefusal)
    assert result.code in {'record_malformed_decision_block', 'record_decision_kind_mismatch'}


def test_no_io_env_clock_model_or_reparse(monkeypatch):
    import builtins
    import socket
    import time
    import hermes_cli.kanban_email_card as parser
    parsed = inspect_email_card(body(RECORD))
    def forbidden(*args, **kwargs):
        raise AssertionError('ambient access or reparse')
    with monkeypatch.context() as scope:
        for owner, name in [(builtins,'open'), (os,'getenv'), (socket,'socket'),
                            (time,'time'), (time,'monotonic'), (parser,'inspect_email_card'),
                            (parser,'parse_decision_block')]:
            scope.setattr(owner, name, forbidden)
        assert validate_record(parsed, own_card_id='t_76543210', subject_permitted=False) is parsed
    source = Path('hermes_cli/kanban_email_record.py').read_text()
    assert 'run_agent' not in source and 'requests' not in source


def test_cross_process_determinism():
    script = '''import sys
from hermes_cli.kanban_email_card import inspect_email_card
from hermes_cli.kanban_email_record import validate_record
print(repr(validate_record(inspect_email_card(sys.stdin.read()), own_card_id='t_76543210', subject_permitted=False)))
'''
    outputs = []
    for seed in ['1', '42']:
        result = subprocess.run([sys.executable, '-c', script], input=body(mutate('counts', None, omit=True)),
                                text=True, capture_output=True, check=True,
                                env={**os.environ, 'PYTHONHASHSEED':seed, 'HERMES_KANBAN_BOARD':seed})
        outputs.append(result.stdout)
    assert outputs[0] == outputs[1]
