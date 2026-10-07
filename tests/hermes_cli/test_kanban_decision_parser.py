"""Decision parser contract; no live board, mailbox or policy resolver."""
from dataclasses import FrozenInstanceError
import builtins
import os
import socket
import time

import pytest

from hermes_cli.kanban_email_card import (
    DecisionBlock, DecisionRefusal, ExpiringDecisionBlock, parse_decision_block,
)

PAYLOAD = '''card_kind: proposal
revision: "opaque 001"
requires: needs_input
options:
  - id: A
    label: "DECISION=A: keep  spaces"
    effect: "unblock -> ready"
    comment: |
      DECISION=A: keep  spaces.
      Recorded authority.
'''


def fenced(payload=PAYLOAD):
    return 'Prose before.\n```decision\n' + payload + '```\nProse after.'


def test_verbatim_and_no_defaults():
    result = parse_decision_block(fenced())
    assert type(result) is DecisionBlock
    assert result.to_dict() == {
        'card_kind': 'proposal', 'revision': 'opaque 001', 'requires': 'needs_input',
        'options': [{'id': 'A', 'label': 'DECISION=A: keep  spaces',
                     'effect': 'unblock -> ready',
                     'comment': 'DECISION=A: keep  spaces.\nRecorded authority.\n'}],
    }
    assert not hasattr(result, 'expires')
    assert result == parse_decision_block(fenced())
    with pytest.raises(FrozenInstanceError):
        result.revision = 'changed'
    with pytest.raises(FrozenInstanceError):
        result.options[0].comment = 'changed'
    exported = result.to_dict()
    exported['options'][0]['comment'] = 'changed'
    assert result.options[0].comment != 'changed'


@pytest.mark.parametrize('expires', ['2026-10-08T12:00:00+08:00', '"2026-10-08T12:00:00Z"'])
def test_optional_expiry_is_not_coerced(expires):
    result = parse_decision_block(fenced(PAYLOAD + 'expires: ' + expires + '\n'))
    assert type(result) is ExpiringDecisionBlock
    assert result.to_dict()['expires'] == expires.strip('"')
    assert result.to_dict().keys() == {'card_kind', 'revision', 'requires', 'options', 'expires'}


CASES = [
    ('missing_fence', 'plain body'),
    ('bad_fence', fenced().replace('```decision', '~~~~decision')),
    ('truncated_block', '```decision\n' + PAYLOAD),
    ('malformed_payload', fenced('options: [\n')),
    ('unknown_key', fenced(PAYLOAD + 'unknown: value\n')),
    ('wrong_type', fenced(PAYLOAD.replace('"opaque 001"', '123'))),
    ('duplicate_option_id', fenced(PAYLOAD + PAYLOAD[PAYLOAD.index('  - id:'): ])),
    ('empty_options', fenced('card_kind: proposal\nrevision: r\nrequires: needs_input\noptions: []\n')),
    ('missing_option_comment', fenced(PAYLOAD[:PAYLOAD.index('    comment:')])),
    ('empty_option_comment', fenced(PAYLOAD[:PAYLOAD.index('    comment:')] + '    comment: ""\n')),
    ('missing_field', fenced(PAYLOAD.replace('requires: needs_input\n', ''))),
    ('unknown_card_kind', fenced(PAYLOAD.replace('proposal', 'alien'))),
    ('duplicate_key', fenced(PAYLOAD + 'revision: other\n')),
    ('multiple_blocks', fenced() + '\n' + fenced()),
    ('yaml_reference', fenced(PAYLOAD.replace('"opaque 001"', '&r opaque'))),
    ('out_of_bounds', fenced(PAYLOAD + '#' + 'x' * 201 + '\n')),
]


@pytest.mark.parametrize('code,body', CASES)
def test_distinct_refusal_classes(code, body):
    result = parse_decision_block(body)
    assert type(result) is DecisionRefusal
    assert result.code == code
    assert result.reason and '\n' not in result.reason
    assert result == parse_decision_block(body)
    assert not hasattr(result, 'options')


def test_refusal_codes_are_distinct():
    assert len({parse_decision_block(body).code for _, body in CASES}) == len(CASES)


@pytest.mark.parametrize('payload', [
    PAYLOAD.replace('"opaque 001"', 'true'),
    PAYLOAD.replace('"opaque 001"', 'null'),
    PAYLOAD.replace('"opaque 001"', '2026-10-08'),
    PAYLOAD.replace('"opaque 001"', '!!int "001"'),
    PAYLOAD.replace('"opaque 001"', '[r]'),
    PAYLOAD.replace('    effect: "unblock -> ready"', '    effect: {nested: value}'),
    PAYLOAD.replace('    effect: "unblock -> ready"', '    content: forbidden'),
    PAYLOAD.replace('  - id: A', '  - id: A\n    id: B'),
    PAYLOAD.replace('    comment: |\n      DECISION=A: keep  spaces.\n      Recorded authority.', '    comment: "   "'),
    PAYLOAD + '\n---\n' + PAYLOAD,
    PAYLOAD.replace('"opaque 001"', '!!python/object:os.system {}'),
    '[]\n', '',
])
def test_other_malformed_inputs_never_partial(payload):
    assert isinstance(parse_decision_block(fenced(payload)), DecisionRefusal)


def test_fence_scoping_and_crlf():
    assert parse_decision_block('````text\n' + fenced() + '\n````').code == 'missing_fence'
    assert parse_decision_block(fenced().replace('\n', '\r\n')) == parse_decision_block(fenced())
    assert parse_decision_block(fenced().replace('```decision', '   ```decision')) == parse_decision_block(fenced())


def test_no_ambient_io_or_clock(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Parser attempted ambient I/O or clock access')
    with monkeypatch.context() as scope:
        scope.setattr(builtins, 'open', forbidden)
        scope.setattr(os, 'getenv', forbidden)
        scope.setattr(socket, 'socket', forbidden)
        scope.setattr(time, 'time', forbidden)
        scope.setattr(time, 'monotonic', forbidden)
        for _, body in CASES:
            assert isinstance(parse_decision_block(body), DecisionRefusal)
        assert isinstance(parse_decision_block(fenced()), DecisionBlock)


def test_edge_inputs():
    for body in [None, 42, b'body']:
        assert isinstance(parse_decision_block(body), DecisionRefusal)
    assert parse_decision_block(fenced(PAYLOAD + '# ' + '\ud800' + '\n')).code == 'malformed_payload'
    assert parse_decision_block(fenced(PAYLOAD + ('# bounded line\n' * 800))).code == 'out_of_bounds'
    assert parse_decision_block(fenced(PAYLOAD[:PAYLOAD.index('    comment:')] + '    comment:\n')).code == 'empty_option_comment'
    assert parse_decision_block(fenced(PAYLOAD.replace('proposal', 'record'))).card_kind == 'record'


def test_all_schema_fields_are_required_without_fill():
    assert parse_decision_block(fenced(PAYLOAD.replace('card_kind: proposal\n', ''))).code == 'missing_card_kind'
    for field in ['revision: "opaque 001"\n', 'requires: needs_input\n',
                  '    label: "DECISION=A: keep  spaces"\n', '    effect: "unblock -> ready"\n']:
        assert parse_decision_block(fenced(PAYLOAD.replace(field, ''))).code == 'missing_field'
