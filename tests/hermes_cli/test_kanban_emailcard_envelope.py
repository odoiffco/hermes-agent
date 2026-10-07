"""Frozen wire grammar v1.2: strict shared parser, no board/clock/I/O."""
import copy
from dataclasses import FrozenInstanceError

import pytest
import yaml

from hermes_cli.kanban_email_card import (
    EmailCardEnvelope, EmailCardRefusal, REQUIRED_PATHS, RECORD_GROUP_PATHS,
    parse_email_card, inspect_email_card, scan_email_content, required_paths,
)

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
STANDING = ['no_move', 'no_archive', 'no_filing', 'content_not_on_board']
RECORD = dict(kind='record', action='no_action', effect='Pass recorded', fences=STANDING,
              evidence=dict(reason='Filer classification', thread_hash='not_reviewed'),
              receipts=dict(counters_after='0|0|0|0|0|0|0|0|0'),
              verification=dict(card_id='t_01234567'), dispute_lane='Independent review',
              counts=dict(passes_in_window=1))
PROPOSAL = dict(kind='proposal', item=dict(message_id='A' * 80), action='draft_prepared',
                effect='Draft preparation', reversibility='reversible_gui',
                fences=STANDING + ['no_send', 'no_reply', 'no_forward'],
                evidence=dict(reason='Preparation rationale', snapshot_hash='a'*64,
                              thread_hash='not_reviewed'),
                receipts=dict(operations=[dict(key='draft', status='pending')]),
                verification=dict(card_id='t_01234567', kind='independent'))


def body(data, prose='', decision=None):
    if decision is None:
        decision = DECISION if data.get('kind') == 'proposal' else ''
    return prose + '\n```emailcard\n' + yaml.safe_dump(data, sort_keys=False, width=100) + '```\n' + decision


def delete(data, path):
    fields = path.split('.')
    target = data
    for field in fields[:-1]:
        target = target[field]
    del target[fields[-1]]


def set_path(data, path, value):
    fields = path.split('.')
    target = data
    for field in fields[:-1]:
        target = target.setdefault(field, {})
    target[fields[-1]] = value


@pytest.mark.parametrize('data', [RECORD, PROPOSAL])
def test_roundtrip_deterministic_immutable(data):
    parsed = parse_email_card(body(data), own_card_id='t_76543210')
    assert isinstance(parsed, EmailCardEnvelope), parsed
    assert parsed.emailcard.to_dict() == data
    assert parsed == parse_email_card(body(data), own_card_id='t_76543210')
    assert parsed.emailcard.get_path('evidence.thread_hash') == 'not_reviewed'
    assert 'expires' not in (parsed.decision.to_dict() if parsed.decision else {})
    exported = parsed.emailcard.to_dict()
    exported['evidence']['reason'] = 'Changed'
    assert parsed.emailcard.evidence.reason != 'Changed'
    with pytest.raises(FrozenInstanceError):
        parsed.emailcard.action = 'mark_read'
    if parsed.emailcard.counts is not None:
        with pytest.raises(TypeError):
            parsed.emailcard.counts['acted'] = 1


@pytest.mark.parametrize('kind,data', [('record', RECORD), ('proposal', PROPOSAL)])
def test_every_required_path_missing_or_empty_named(kind, data):
    for path in REQUIRED_PATHS[kind]:
        for empty in (None, '', [], {}):
            changed = copy.deepcopy(data)
            if empty is None:
                delete(changed, path)
            else:
                set_path(changed, path, empty)
            result = parse_email_card(body(changed, decision=DECISION if kind == 'proposal' else ''))
            assert isinstance(result, EmailCardRefusal), (path, empty, result)
            assert result.path == path, (path, empty, result)
            assert path in result.reason
            # Consumers can examine omissions without ever re-parsing prose.
            inspected = inspect_email_card(body(changed, decision=DECISION if kind == 'proposal' else ''))
            if empty is None and path != 'kind':
                assert isinstance(inspected, EmailCardEnvelope), inspected
                assert path in [issue.path for issue in inspected.required_issues]


@pytest.mark.parametrize('key', ['acted', 'tagged_review', 'tagged_action', 'tags_cleared', 'marked_read', 'drafts_prepared'])
def test_record_act_receipt_trigger(key):
    changed = copy.deepcopy(RECORD)
    changed['counts'][key] = 1
    assert parse_email_card(body(changed)).path == 'receipts.operations'
    changed['receipts']['operations'] = []
    assert parse_email_card(body(changed)).path == 'receipts.operations'
    changed['receipts']['operations'] = [dict(key='act', status='verified')]
    assert isinstance(parse_email_card(body(changed)), EmailCardEnvelope)
    assert 'receipts.operations' in required_paths(parse_email_card(body(changed)).emailcard)
    changed['counts'][key] = 0
    changed['receipts']['operations'] = []
    assert isinstance(parse_email_card(body(changed)), EmailCardEnvelope)


@pytest.mark.parametrize('path,value', [
    ('kind','other'), ('item.message_id','short'), ('item.message_id','A'*401),
    ('item.message_id','.'*20), ('item.conversation_id','?'*20),
    ('item.folder','sent'), ('item.folder','other:'+'x'*65),
    ('item.received_date','2026-02-30'), ('item.received_date','2026-10-07T12:00:00-05:00'),
    ('item.sender_domain','UPPER.example'), ('item.sender_domain','bad_.example'),
    ('item.sender_display','Person@example'), ('item.sender_display','x'*65),
    ('item.subject','x'*121), ('item.subject','line\nbreak'),
    ('action','send'), ('effect','x'*201), ('effect','a\nb'),
    ('reversibility','undo'), ('fences',['alien']), ('fences','no_move'),
    ('evidence.reason','x'*2001), ('evidence.snapshot_hash','A'*64),
    ('evidence.thread_hash','unknown'), ('receipts.counters_before','1|2'),
    ('receipts.counters_after','0|0|0|0|0|0|0|0|true'),
    ('receipts.operations',[dict(key='x',status='done')]),
    ('receipts.operations',[dict(key='x'*121,status='pending')]),
    ('receipts.operations',[dict(key='x',status='pending')]*9),
    ('receipts.attention_readback',{'Other':1}), ('receipts.attention_readback',{'Action':True}),
    ('verification.card_id','promise'), ('verification.kind','self'),
    ('dispute_lane','x'*401), ('counts',{'other':1}), ('counts',{'acted':True}),
])
def test_wrong_types_bounds_and_enums(path, value):
    changed = copy.deepcopy(RECORD)
    set_path(changed, path, value)
    result = parse_email_card(body(changed), subject_permitted=True)
    assert isinstance(result, EmailCardRefusal), (path, result)


@pytest.mark.parametrize('path', ['extra', 'item.extra', 'evidence.extra', 'receipts.extra', 'verification.extra', 'counts.extra'])
def test_closed_nested_keys(path):
    changed = copy.deepcopy(RECORD)
    set_path(changed, path, 'unknown')
    result = parse_email_card(body(changed))
    assert result.code == 'unknown_key'
    assert result.path == path


@pytest.mark.parametrize('name', ['body','bodyPreview','html','text','extraction','content','content_base64','draft_body','document','attachment_content'])
@pytest.mark.parametrize('container', ['emailcard', 'decision'])
def test_forbidden_keys_anywhere(name, container):
    if container == 'emailcard':
        changed = copy.deepcopy(RECORD)
        changed['unknown'] = {'deep': [{name:'secret'}]}
        text = body(changed)
    else:
        text = body(RECORD) + DECISION.replace('comment:', f'{name}: secret\n    comment:')
    context = scan_email_content(text)
    assert any(issue.code == 'forbidden_content_key' for issue in context.issues)
    assert parse_email_card(text).code == 'forbidden_content_key'


@pytest.mark.parametrize('prose,code', [
    ('A'*80, 'base64_content_run'), ('```text\n'+'!'*401+'\n```', 'oversized_prose_fence'),
    ('> '+'!'*198, 'overlong_quoted_line'),
])
def test_content_context_without_prose_reparse(prose, code):
    text = body(RECORD, prose)
    inspected = inspect_email_card(text)
    assert isinstance(inspected, EmailCardEnvelope)
    assert code in [issue.code for issue in inspected.content.issues]
    assert parse_email_card(text).code == code


def test_base64_field_exemptions_are_narrow_and_yaml_semantic():
    assert isinstance(parse_email_card(body(PROPOSAL)), EmailCardEnvelope)
    changed = copy.deepcopy(RECORD)
    changed['evidence']['reason'] = 'A'*80
    assert parse_email_card(body(changed)).code == 'base64_content_run'
    # Escaping/chunking must not bypass semantic scalar scanning.
    text = body(RECORD).replace('Filer classification', '"' + 'A'*79 + '\\x41' + '"')
    assert scan_email_content(text).issues[0].code == 'base64_content_run'
    assert scan_email_content(body(RECORD) + '\n# ' + 'A'*80).issues[0].code == 'base64_content_run'


def test_fences_verification_subject_and_kind_scope():
    changed = copy.deepcopy(RECORD)
    changed['fences'] = ['content_not_on_board']
    assert parse_email_card(body(changed)).code == 'missing_action_fence'
    assert parse_email_card(body(RECORD), own_card_id='t_01234567').code == 'self_verification'
    changed = copy.deepcopy(PROPOSAL)
    changed['item']['subject'] = 'Bounded header'
    assert parse_email_card(body(changed)).code == 'subject_not_permitted_by_board_policy'
    assert isinstance(parse_email_card(body(changed), subject_permitted=True), EmailCardEnvelope)
    changed['counts'] = {'judged':1}
    assert parse_email_card(body(changed), subject_permitted=True).code == 'kind_scope_violation'
    assert parse_email_card(body(PROPOSAL, decision='')).code == 'missing_decision_block'
    assert parse_email_card(body(PROPOSAL, decision=DECISION.replace('proposal','record'))).code == 'decision_kind_mismatch'


def test_all_optional_paths_and_authored_empty_values():
    changed = copy.deepcopy(RECORD)
    changed['item'] = dict(message_id='A'*20, conversation_id='B'*20, folder='other:Sent',
                           received_date='2026-10-07T12:00:00+08:00', sender_domain='example.org',
                           sender_display="O'Doige", subject='')
    changed['reversibility'] = 'not_applicable'
    changed['evidence']['snapshot_hash'] = 'a'*64
    changed['receipts'].update(counters_before='01|-2|3|4|5|6|7|8|9',
                               operations=[], attention_readback={'Review':1, 'Action':2, 'null':0})
    parsed = parse_email_card(body(changed), subject_permitted=True)
    assert isinstance(parsed, EmailCardEnvelope), parsed
    assert parsed.emailcard.to_dict() == changed
    assert parsed.emailcard.receipts.counters_before_vector == (1,-2,3,4,5,6,7,8,9)
    assert RECORD_GROUP_PATHS['acts_applied'] == ('action','counts','receipts.operations')


@pytest.mark.parametrize('mutation,code', [
    (lambda s:s.replace('kind: record','kind: record\nkind: record'), 'duplicate_key'),
    (lambda s:s.replace('effect: Pass recorded','effect: &e Pass recorded'), 'yaml_reference'),
    (lambda s:s.replace('effect: Pass recorded','effect: !!int 1'), 'wrong_type'),
    (lambda s:s.replace('```emailcard','~~~~emailcard'), 'bad_fence'),
    (lambda s:s.replace('```emailcard','```emailcard extra'), 'bad_fence'),
    (lambda s:s+body(RECORD), 'multiple_emailcard_blocks'),
    (lambda s:s.replace('```\n','',1), 'truncated_block'),
    (lambda s:s.replace('kind: record','kind: record\n'+('# !\n'*2100)), 'out_of_bounds'),
    (lambda s:s.replace('kind: record','kind: record\n# '+('!'*201)), 'out_of_bounds'),
])
def test_wire_structure(mutation, code):
    result = parse_email_card(mutation(body(RECORD)))
    assert isinstance(result, EmailCardRefusal), result
    assert result.code == code


def test_scalar_safety_no_fill_no_ambient_calls(monkeypatch):
    import builtins
    import os
    import socket
    import time
    def forbidden(*args, **kwargs):
        raise AssertionError('ambient access')
    with monkeypatch.context() as scope:
        for owner, name in [(builtins,'open'),(os,'getenv'),(socket,'socket'),(time,'time'),(time,'monotonic')]:
            scope.setattr(owner,name,forbidden)
        parsed = parse_email_card(body(RECORD))
        assert parsed.emailcard.item is None
        assert parsed.emailcard.to_dict() == RECORD
    for value in [None, 42, b'body', '\ud800']:
        assert isinstance(parse_email_card(value), EmailCardRefusal)
    assert parse_email_card('plain').code == 'missing_emailcard_block'


@pytest.mark.parametrize('action,addition', [
    ('no_action', []), ('tag_review', ['no_builtin_category_change']),
    ('tag_action', ['no_builtin_category_change']), ('clear_tag', ['no_builtin_category_change']),
    ('mark_read', ['no_read_receipt']), ('draft_prepared', ['no_send','no_reply','no_forward']),
])
def test_every_action_fence_mapping(action, addition):
    changed = copy.deepcopy(RECORD)
    changed['action'] = action
    changed['fences'] = STANDING + addition
    assert isinstance(parse_email_card(body(changed)), EmailCardEnvelope)
    for fence in changed['fences']:
        omitted = copy.deepcopy(changed)
        omitted['fences'].remove(fence)
        result = parse_email_card(body(omitted))
        assert result.code == 'missing_action_fence'
        assert fence in result.reason


def test_exact_payload_and_line_boundaries():
    payload = yaml.safe_dump(RECORD, sort_keys=False)
    # Fill to the exact byte bound with non-content comments, all short lines.
    while len(payload.encode()) < 8192:
        remaining = 8192 - len(payload.encode())
        payload += '#'+ '!'*(min(remaining, 200)-2) + '\n' if remaining >= 2 else '\n'
    assert len(payload.encode()) == 8192
    assert isinstance(parse_email_card('```emailcard\n'+payload+'```'), EmailCardEnvelope)
    assert parse_email_card('```emailcard\n'+payload+'\n```').code == 'out_of_bounds'
    base = yaml.safe_dump(RECORD, sort_keys=False)
    assert isinstance(parse_email_card('```emailcard\n'+base+'#'+'!'*199+'\n```'), EmailCardEnvelope)
    assert parse_email_card('```emailcard\n'+base+'#'+'!'*200+'\n```').code == 'out_of_bounds'
    # UTF-8 byte bound, not merely character count.
    unicode_payload = payload[:-4] + '#中\n'
    assert len(unicode_payload) < len(unicode_payload.encode())
    assert parse_email_card('```emailcard\n'+unicode_payload+'```').code == 'out_of_bounds'


@pytest.mark.parametrize('length', [20, 400, 401])
def test_identifier_semantic_bounds_with_continued_scalar(length):
    text = body(PROPOSAL)
    # Backslash continuation is YAML string syntax, not whitespace in the id.
    identifier = 'A'*length
    encoded = '"' + '\\\n    '.join(identifier[i:i+100] for i in range(0,length,100)) + '"'
    text = text.replace('A'*80, encoded)
    parsed = parse_email_card(text)
    if length <= 400:
        assert isinstance(parsed, EmailCardEnvelope), parsed
        assert parsed.emailcard.item.message_id == identifier
    else:
        assert parsed.path == 'item.message_id'


@pytest.mark.parametrize('receipt', [
    {'key':'', 'status':'pending'}, {'key':'x'}, {'status':'pending'},
    {'key':'x', 'status':'pending', 'extra':'no'},
])
def test_operation_closed_required_paths(receipt):
    changed = copy.deepcopy(PROPOSAL)
    changed['receipts']['operations'] = [receipt]
    assert isinstance(parse_email_card(body(changed)), EmailCardRefusal)


def test_content_threshold_positive_controls_and_decision_scalar_scan():
    assert isinstance(parse_email_card(body(RECORD, 'A'*79)), EmailCardEnvelope)
    assert isinstance(parse_email_card(body(RECORD, '> '+'!'*197)), EmailCardEnvelope)
    assert isinstance(parse_email_card(body(RECORD, '```text\n'+'!'*399+'\n```')), EmailCardEnvelope)
    text = body(PROPOSAL, decision=DECISION.replace("'DECISION=A: prepare'", "'"+'A'*80+"'"))
    assert parse_email_card(text).code == 'base64_content_run'
    changed = copy.deepcopy(RECORD)
    changed['receipts']['attention_readback'] = {'null':0}
    assert isinstance(parse_email_card(body(changed).replace("'null':", 'null:')), EmailCardEnvelope)


def test_missing_parent_named_leaves_and_no_optional_expiry_fill():
    changed = copy.deepcopy(RECORD)
    del changed['evidence']
    inspected = inspect_email_card(body(changed))
    assert [i.path for i in inspected.required_issues] == ['evidence.reason','evidence.thread_hash']
    assert 'evidence' not in inspected.emailcard.to_dict()
    text = body(PROPOSAL, decision=DECISION.replace('revision: r', 'revision: r\nexpires: 2026-10-08T12:00:00Z'))
    parsed = parse_email_card(text)
    assert parsed.decision.expires == '2026-10-08T12:00:00Z'
    assert 'expires' not in parse_email_card(body(PROPOSAL)).decision.to_dict()


@pytest.mark.parametrize('date_value', ['2026-10-07', '2026-10-07T12:30Z', '2026-10-07T12:30:59.123Z'])
def test_iso_received_date_keeps_authored_string(date_value):
    changed = copy.deepcopy(RECORD)
    changed['item'] = {'received_date': date_value}
    parsed = parse_email_card(body(changed))
    assert parsed.emailcard.item.received_date == date_value


@pytest.mark.parametrize('path', [
    'item.message_id','item.conversation_id','item.folder','item.received_date',
    'item.sender_domain','item.sender_display','item.subject','action','effect',
    'reversibility','evidence.reason','evidence.snapshot_hash','evidence.thread_hash',
    'receipts.counters_before','receipts.counters_after','verification.card_id',
    'verification.kind','dispute_lane',
])
@pytest.mark.parametrize('value', [None, True, 123, [], {}])
def test_scalar_paths_reject_nonstring_types(path, value):
    changed = copy.deepcopy(RECORD)
    set_path(changed, path, value)
    assert isinstance(parse_email_card(body(changed), subject_permitted=True), EmailCardRefusal)


@pytest.mark.parametrize('path', ['item','evidence','receipts','verification','counts'])
@pytest.mark.parametrize('value', ['not a map', True, 123, []])
def test_map_paths_reject_nonmap_types(path, value):
    changed = copy.deepcopy(RECORD)
    changed[path] = value
    assert isinstance(parse_email_card(body(changed)), EmailCardRefusal)


def test_decision_errors_remain_visible_to_consumers():
    text = body(PROPOSAL, decision=DECISION.replace("    comment: 'DECISION=A: prepare'\n", ''))
    inspected = inspect_email_card(text)
    assert isinstance(inspected, EmailCardEnvelope)
    assert inspected.decision.code == 'missing_option_comment'
    assert parse_email_card(text).code == 'malformed_decision_block'
    text = body(PROPOSAL, decision=DECISION + DECISION)
    assert parse_email_card(text).code == 'malformed_decision_block'
    assert parse_email_card(body(RECORD), subject_permitted='true').code == 'wrong_type'
