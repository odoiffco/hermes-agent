"""Pure emailcard envelope and content inspection, frozen wire grammar v1.2.

inspect_email_card retains missing paths and R3–R6 findings for kind validators;
parse_email_card is the strict convenience entrypoint. Neither resolves a board.
Policy and self-verification identity must be supplied by the caller. Authored
strings (including counters and dates) round-trip without coercion or filling.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import date, datetime
import re
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, Mapping, NoReturn

if TYPE_CHECKING:
    from hermes_cli.kanban_email_card import DecisionBlock, DecisionRefusal

import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode
from yaml.tokens import AliasToken, AnchorToken

Kind = Literal['record', 'proposal']
Action = Literal['tag_review', 'tag_action', 'clear_tag', 'mark_read', 'draft_prepared', 'no_action']
Reversibility = Literal['reversible_override', 'reversible_gui', 'irreversible_receipt', 'not_applicable']
Status = Literal['verified', 'pending', 'refused']

STANDING_FENCES = frozenset({'no_move', 'no_archive', 'no_filing', 'content_not_on_board'})
ACTION_FENCES = MappingProxyType({
    'no_action': STANDING_FENCES,
    **{a: STANDING_FENCES | {'no_builtin_category_change'} for a in ('tag_review','tag_action','clear_tag')},
    'mark_read': STANDING_FENCES | {'no_read_receipt'},
    'draft_prepared': STANDING_FENCES | {'no_send','no_reply','no_forward'},
})
FENCE_KEYS = STANDING_FENCES | {'no_send','no_reply','no_forward','no_builtin_category_change','no_read_receipt'}
COUNT_KEYS = frozenset({'judged','inert','acted','tagged_review','tagged_action','tags_cleared',
                        'marked_read','drafts_prepared','refusals','escalated','passes_in_window'})
ACTING_COUNT_KEYS = ('acted','tagged_review','tagged_action','tags_cleared','marked_read','drafts_prepared')
COUNTER_FIELDS = ('pending','operations','effects','drafts','draft_bindings','write_refusals',
                  'attention_non_null','action','review')
ATTENTION_KEYS = frozenset({'Review','Action','null'})
FORBIDDEN_CONTENT_KEYS = frozenset({'body','bodyPreview','html','text','extraction','content',
                                  'content_base64','draft_body','document','attachment_content'})
# Only identifiers/hash references have an explicit need for long encoded runs.
# Rationale, effects, decision comments, subject and prose are not exemptions.
BASE64_PERMITTED_PATHS = frozenset({'item.message_id','item.conversation_id',
                                   'evidence.snapshot_hash','evidence.thread_hash'})
REQUIRED_PATHS = MappingProxyType({
    'record': ('kind','action','effect','fences','evidence.reason','evidence.thread_hash',
               'receipts.counters_after','verification.card_id','dispute_lane','counts'),
    'proposal': ('kind','item.message_id','action','effect','reversibility','fences',
                 'evidence.reason','evidence.snapshot_hash','evidence.thread_hash',
                 'receipts.operations','verification.card_id'),
})
RECORD_GROUP_PATHS = MappingProxyType({
    'counts': ('counts',), 'acts_applied': ('action','counts','receipts.operations'),
    'evidence_basis': ('evidence.reason','evidence.thread_hash'),
    'receipts': ('receipts.counters_after','receipts.operations'),
    'dispute_lane': ('dispute_lane',),
})


@dataclass(frozen=True)
class EmailCardRefusal:
    code: str
    reason: str
    path: str = ''


class _EnvelopeInvalid(Exception):
    def __init__(self, code, path, reason):
        self.refusal = EmailCardRefusal(code, f'{path}: {reason}', path)


def _fail(code, path, reason) -> NoReturn:
    raise _EnvelopeInvalid(code, path, reason)


def _export(value):
    if isinstance(value, _AuthoredFields):
        return value.to_dict()
    if isinstance(value, Mapping):
        return {key: _export(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_export(item) for item in value]
    return value


@dataclass(frozen=True)
class _AuthoredFields:
    def to_dict(self) -> dict:
        # None is absence, never an authored YAML null (which schema refuses).
        return {f.name: _export(getattr(self, f.name)) for f in fields(self)
                if getattr(self, f.name) is not None}


@dataclass(frozen=True)
class EmailItem(_AuthoredFields):
    message_id: str | None = None
    conversation_id: str | None = None
    folder: str | None = None
    received_date: str | None = None
    sender_domain: str | None = None
    sender_display: str | None = None
    subject: str | None = None


@dataclass(frozen=True)
class EmailEvidence(_AuthoredFields):
    reason: str | None = None
    snapshot_hash: str | None = None
    thread_hash: str | None = None


@dataclass(frozen=True)
class EmailOperation(_AuthoredFields):
    key: str
    status: Status


@dataclass(frozen=True)
class EmailReceipts(_AuthoredFields):
    counters_before: str | None = None
    counters_after: str | None = None
    operations: tuple[EmailOperation, ...] | None = None
    attention_readback: Mapping[str, int] | None = None

    @property
    def counters_before_vector(self) -> tuple[int, ...] | None:
        return None if self.counters_before is None else tuple(map(int, self.counters_before.split('|')))

    @property
    def counters_after_vector(self) -> tuple[int, ...] | None:
        return None if self.counters_after is None else tuple(map(int, self.counters_after.split('|')))


@dataclass(frozen=True)
class EmailVerification(_AuthoredFields):
    card_id: str | None = None
    kind: Literal['independent'] | None = None


@dataclass(frozen=True)
class EmailCard(_AuthoredFields):
    kind: Kind
    item: EmailItem | None = None
    action: Action | None = None
    effect: str | None = None
    reversibility: Reversibility | None = None
    fences: tuple[str, ...] | None = None
    evidence: EmailEvidence | None = None
    receipts: EmailReceipts | None = None
    verification: EmailVerification | None = None
    dispute_lane: str | None = None
    counts: Mapping[str, int] | None = None

    def get_path(self, path: str):
        """Typed authored value, or None for absence; never synthesizes a field."""
        current = self
        for part in path.split('.'):
            current = current.get(part) if isinstance(current, Mapping) else getattr(current, part, None)
            if current is None:
                break
        return current


def required_paths(card: EmailCard) -> tuple[str, ...]:
    """§9.1.3/§9.2/§9.3 mandatory paths, including the acting-record trigger."""
    paths = REQUIRED_PATHS[card.kind]
    if card.kind == 'record' and card.counts is not None and any(
            card.counts.get(key, 0) >= 1 for key in ACTING_COUNT_KEYS):
        paths += ('receipts.operations',)
    return paths


def required_field_issues(card: EmailCard) -> tuple[EmailCardRefusal, ...]:
    return tuple(EmailCardRefusal('missing_required_field', f'{path}: required non-empty authored field', path)
                 for path in required_paths(card) if _empty(card.get_path(path)))


def _empty(value):
    return value is None or value == '' or isinstance(value, str) and not value.strip() or (
        isinstance(value, (tuple, Mapping)) and not value)


@dataclass(frozen=True)
class ContentIssue:
    code: str
    path: str
    line: int
    length: int = 0

    def to_refusal(self):
        # Do not copy message content into a refusal/log.
        return EmailCardRefusal(self.code, f'{self.path}: content refusal at line {self.line}', self.path)


@dataclass(frozen=True)
class ContentCheckContext:
    issues: tuple[ContentIssue, ...]


@dataclass(frozen=True)
class EmailCardEnvelope:
    emailcard: EmailCard
    decision: DecisionBlock | DecisionRefusal | None
    content: ContentCheckContext
    required_issues: tuple[EmailCardRefusal, ...]


@dataclass(frozen=True)
class _FenceBlock:
    info: str
    payload: str
    line: int
    machine: bool


_FENCE = re.compile(r' {0,3}(`{3,}|~{3,})([^\r\n]*)\r?\n?')
_RUN = re.compile(r'[A-Za-z0-9+/=]{80,}')


def _containers(body):
    blocks, prose = [], []
    active = None
    lines = []
    for number, line in enumerate(body.splitlines(keepends=True), 1):
        match = _FENCE.fullmatch(line)
        if active is None:
            if match:
                marker, info = match.groups()
                info = info.strip()
                machine = marker == '```' and info in {'emailcard','decision'}
                if any(info.startswith(name) for name in ('emailcard','decision')) and not machine:
                    _fail('bad_fence', info.split()[0], 'exact triple-backtick container required')
                active = (marker, info, number + 1, machine)
                lines = []
            else:
                prose.append((number, line))
        elif match and match.group(1)[0] == active[0][0] and len(match.group(1)) >= len(active[0]) and not match.group(2).strip():
            marker, info, start, machine = active
            if machine and match.group(1) != '```':
                _fail('bad_fence', info, 'exact closing fence required')
            blocks.append(_FenceBlock(info, ''.join(lines), start, machine))
            active = None
        else:
            lines.append(line)
    if active:
        marker, info, start, machine = active
        if machine:
            _fail('truncated_block', info, 'closing fence missing')
        blocks.append(_FenceBlock(info, ''.join(lines), start, False))
    return blocks, prose


def _document(payload, name):
    try:
        size = len(payload.encode('utf-8'))
    except UnicodeError:
        _fail('malformed_payload', name, 'valid Unicode required')
    if size > 8192 or any(len(line) > 200 for line in payload.splitlines()):
        _fail('out_of_bounds', name, '8 KiB / 200-character line bound exceeded')
    try:
        if any(isinstance(token, (AliasToken, AnchorToken)) for token in yaml.scan(payload)):
            _fail('yaml_reference', name, 'anchors and aliases refused')
        return yaml.compose(payload, Loader=yaml.SafeLoader)
    except (yaml.YAMLError, RecursionError):
        _fail('malformed_payload', name, 'complete YAML document required')


def _node_content(node, name, path='', depth=0):
    """Walk even unknown maps so R3 is not hidden behind R1; scalar semantic scan."""
    if depth > 64:
        _fail('malformed_payload', name, 'excessive nested structure')
    issues, spans = [], []
    if isinstance(node, MappingNode):
        for key, value in node.value:
            part = key.value if isinstance(key, ScalarNode) else '<key>'
            child_path = f'{path}.{part}' if path else part
            if part in FORBIDDEN_CONTENT_KEYS:
                issues.append(ContentIssue('forbidden_content_key', f'{name}.{child_path}', key.start_mark.line))
            for child, child_name in ((key, child_path + '.<key>'), (value, child_path)):
                found, exempt = _node_content(child, name, child_name, depth + 1)
                issues.extend(found)
                spans.extend(exempt)
    elif isinstance(node, SequenceNode):
        for i, child in enumerate(node.value):
            found, exempt = _node_content(child, name, f'{path}[{i}]', depth + 1)
            issues.extend(found)
            spans.extend(exempt)
    elif isinstance(node, ScalarNode):
        permitted = name == 'emailcard' and path in BASE64_PERMITTED_PATHS and node.tag == 'tag:yaml.org,2002:str'
        if permitted:
            spans.append((node.start_mark.index, node.end_mark.index))
        else:
            for match in _RUN.finditer(node.value):
                issues.append(ContentIssue('base64_content_run', f'{name}.{path}', node.start_mark.line, len(match.group())))
    return issues, spans


def _content_context(blocks, prose):
    issues = []
    for number, line in prose:
        if re.match(r' {0,3}>', line) and len(line.rstrip('\r\n')) >= 200:
            issues.append(ContentIssue('overlong_quoted_line', 'prose', number, len(line.rstrip('\r\n'))))
        for run in _RUN.finditer(line):
            issues.append(ContentIssue('base64_content_run', 'prose', number, len(run.group())))
    for block in blocks:
        raw = block.payload
        if block.machine:
            try:
                node = _document(raw, block.info)
                found, spans = _node_content(node, block.info)
                issues.extend(ContentIssue(i.code, i.path, block.line + i.line, i.length) for i in found)
                masked = list(raw)
                for start, end in spans:
                    masked[start:end] = [' ' if c not in '\r\n' else c for c in raw[start:end]]
                raw = ''.join(masked)
            except _EnvelopeInvalid:
                # Structural refusal is returned by inspect; raw scanning still
                # checks encoded runs without treating invalid fields as exempt.
                pass
        elif len(raw) > 400:
            issues.append(ContentIssue('oversized_prose_fence', 'prose', block.line, len(raw)))
        for match in _RUN.finditer(raw):
            issues.append(ContentIssue('base64_content_run', block.info or 'prose',
                                       block.line + raw[:match.start()].count('\n'), len(match.group())))
    priority = {'forbidden_content_key':0, 'base64_content_run':1, 'oversized_prose_fence':2, 'overlong_quoted_line':3}
    return ContentCheckContext(tuple(sorted(set(issues), key=lambda i:(priority[i.code], i.line, i.path, i.length))))


def scan_email_content(body: str) -> ContentCheckContext | EmailCardRefusal:
    """R3–R6 context once, for both validators; no prose paraphrase guarantee."""
    try:
        if not isinstance(body, str):
            _fail('wrong_type', 'body', 'string required')
        body.encode('utf-8')
        return _content_context(*_containers(body))
    except UnicodeError:
        return EmailCardRefusal('malformed_payload','body: valid Unicode required','body')
    except _EnvelopeInvalid as exc:
        return exc.refusal


def _map(node, allowed, path, *, null_key=False):
    if not isinstance(node, MappingNode) or node.tag != 'tag:yaml.org,2002:map':
        _fail('wrong_type', path, 'map required')
    result = {}
    for key, value in node.value:
        if not isinstance(key, ScalarNode):
            _fail('wrong_type', path, 'scalar string key required')
        name = key.value
        literal_null = null_key and key.tag == 'tag:yaml.org,2002:null' and name == 'null'
        if key.tag != 'tag:yaml.org,2002:str' and not literal_null:
            _fail('wrong_type', path, 'string key required')
        full = f'{path}.{name}' if path else name
        if len(name) > 32:
            _fail('out_of_bounds', full, 'key exceeds 32 characters')
        if name in result:
            _fail('duplicate_key', full, 'duplicate key refused')
        if name not in allowed:
            _fail('unknown_key', full, 'unknown key refused')
        result[name] = value
    return result


def _str(node, path, bound=None, *, timestamp=False, newline=True):
    tags = {'tag:yaml.org,2002:str'} | ({'tag:yaml.org,2002:timestamp'} if timestamp else set())
    if not isinstance(node, ScalarNode) or node.tag not in tags:
        _fail('wrong_type', path, 'string required')
    value = node.value
    if bound is not None and len(value) > bound or not newline and ('\n' in value or '\r' in value):
        _fail('out_of_bounds', path, 'string length/newline bound violated')
    return value


def _enum(node, path, allowed):
    value = _str(node, path)
    if value not in allowed:
        _fail('out_of_bounds', path, 'value outside closed enum')
    return value


def _match(node, path, pattern, bound=None):
    value = _str(node, path, bound)
    if not re.fullmatch(pattern, value):
        _fail('out_of_bounds', path, 'value outside permitted format')
    return value


def _int(node, path):
    # YAML booleans, hex, sexagesimal etc. are not decimal integer counters.
    if not isinstance(node, ScalarNode) or node.tag != 'tag:yaml.org,2002:int' or not re.fullmatch(r'[+-]?[0-9]+', node.value):
        _fail('wrong_type', path, 'decimal integer required')
    return int(node.value, 10)


def _sequence(node, path, bound=None):
    if not isinstance(node, SequenceNode) or node.tag != 'tag:yaml.org,2002:seq':
        _fail('wrong_type', path, 'list required')
    if bound is not None and len(node.value) > bound:
        _fail('out_of_bounds', path, 'list bound violated')
    return node.value


def _folder(node, path):
    value = _str(node, path)
    if value not in {'inbox','unknown'} and not re.fullmatch(r'other:[^\r\n]{1,64}', value):
        _fail('out_of_bounds', path, 'folder outside closed enum')
    return value


def _received(node, path):
    value = _str(node, path, timestamp=True)
    try:
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            date.fromisoformat(value)
        elif re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|\+08:00)', value):
            datetime.fromisoformat(value.replace('Z','+00:00'))
        else:
            raise ValueError()
    except ValueError:
        _fail('out_of_bounds', path, 'ISO date or date-time with Z/+08:00 required')
    return value


def _domain(node, path):
    return _match(node, path, r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*', 253)


def _name(node, path):
    value = _str(node, path, 64)
    if any(not c.isalpha() and c not in " .'-" for c in value):
        _fail('out_of_bounds', path, 'sender name charset violated')
    return value


def _counters(node, path):
    return _match(node, path, r'[+-]?[0-9]+(?:\|[+-]?[0-9]+){8}')


def _operations(node, path):
    result = []
    for i, child in enumerate(_sequence(node, path, 8)):
        here = f'{path}[{i}]'
        raw = _map(child, {'key','status'}, here)
        for name in ('key','status'):
            if name not in raw:
                _fail('missing_required_field', f'{here}.{name}', 'required operation field missing')
        key = _str(raw['key'], f'{here}.key', 120)
        if not key.strip():
            _fail('missing_required_field', f'{here}.key', 'required non-empty operation key')
        result.append(EmailOperation(key,
                                     _enum(raw['status'], f'{here}.status', {'verified','pending','refused'})))
    return tuple(result)


def _integer_map(node, path, keys, bound, *, null_key=False):
    raw = _map(node, keys, path, null_key=null_key)
    if len(raw) > bound:
        _fail('out_of_bounds', path, 'map bound violated')
    return MappingProxyType({key:_int(value, f'{path}.{key}') for key,value in raw.items()})


def _object(node, path, cls, specs):
    raw = _map(node, specs.keys(), path)
    return cls(**{key:specs[key](value, f'{path}.{key}') for key,value in raw.items()})


ITEM_SPECS = {
    'message_id': lambda n,p:_match(n,p,r'[A-Za-z0-9_=+/-]{20,400}'),
    'conversation_id': lambda n,p:_match(n,p,r'[A-Za-z0-9_=+/-]{20,400}'),
    'folder': _folder, 'received_date': _received, 'sender_domain': _domain,
    'sender_display': _name, 'subject': lambda n,p:_str(n,p,120,newline=False),
}
EVIDENCE_SPECS = {
    'reason': lambda n,p:_str(n,p,2000),
    'snapshot_hash': lambda n,p:_match(n,p,r'[0-9a-f]{64}'),
    'thread_hash': lambda n,p:_match(n,p,r'(?:[0-9a-f]{64}|not_reviewed)'),
}
RECEIPT_SPECS = {
    'counters_before': _counters, 'counters_after': _counters, 'operations': _operations,
    'attention_readback': lambda n,p:_integer_map(n,p,ATTENTION_KEYS,8,null_key=True),
}
VERIFICATION_SPECS = {
    'card_id': lambda n,p:_match(n,p,r't_[0-9a-f]{8}'),
    'kind': lambda n,p:_enum(n,p,{'independent'}),
}
EMAIL_SPECS = {
    'kind': lambda n,p:_enum(n,p,{'record','proposal'}),
    'item': lambda n,p:_object(n,p,EmailItem,ITEM_SPECS),
    'action': lambda n,p:_enum(n,p,ACTION_FENCES.keys()),
    'effect': lambda n,p:_str(n,p,200,newline=False),
    'reversibility': lambda n,p:_enum(n,p,{'reversible_override','reversible_gui','irreversible_receipt','not_applicable'}),
    'fences': lambda n,p:tuple(_enum(v,p,FENCE_KEYS) for v in _sequence(n,p)),
    'evidence': lambda n,p:_object(n,p,EmailEvidence,EVIDENCE_SPECS),
    'receipts': lambda n,p:_object(n,p,EmailReceipts,RECEIPT_SPECS),
    'verification': lambda n,p:_object(n,p,EmailVerification,VERIFICATION_SPECS),
    'dispute_lane': lambda n,p:_str(n,p,400),
    'counts': lambda n,p:_integer_map(n,p,COUNT_KEYS,12),
}


def _raw_path(raw, path):
    current = raw
    for part in path.split('.'):
        if not isinstance(current, MappingNode):
            return None
        current = next((v for k,v in current.value if isinstance(k,ScalarNode) and k.value == part), None)
    return current


def _node_empty(node):
    return node is None or isinstance(node,ScalarNode) and (node.tag == 'tag:yaml.org,2002:null' or not node.value.strip()) or isinstance(node,(MappingNode,SequenceNode)) and not node.value


def inspect_email_card(body: str) -> EmailCardEnvelope | EmailCardRefusal:
    """Parse shape/types without enforcing required paths or content/policy verdicts.

    Missing/empty fields are exposed as required_issues, never filled. Wrong
    types/unknown keys cannot become a typed envelope. The shared scanner carries
    R3–R6 issues; decision parse refusals are retained for R10 consumers.
    """
    from hermes_cli.kanban_email_card import parse_decision_block
    try:
        if not isinstance(body,str):
            _fail('wrong_type','body','string required')
        body.encode('utf-8')
        blocks, prose = _containers(body)
        email = [b for b in blocks if b.machine and b.info == 'emailcard']
        if len(email) != 1:
            _fail('missing_emailcard_block' if not email else 'multiple_emailcard_blocks', 'emailcard', 'exactly one container required')
        context = _content_context(blocks, prose)
        forbidden = next((i for i in context.issues if i.code == 'forbidden_content_key'), None)
        if forbidden:
            return forbidden.to_refusal()
        node = _document(email[0].payload,'emailcard')
        raw = _map(node, EMAIL_SPECS.keys(), '')
        if 'kind' not in raw or _node_empty(raw['kind']):
            _fail('missing_required_field','kind','required non-empty authored field')
        kind = EMAIL_SPECS['kind'](raw['kind'],'kind')
        # Empty mandatory values must receive the exact required-path refusal,
        # not be coerced into valid values or hidden under a generic type error.
        for path in REQUIRED_PATHS[kind]:
            value = _raw_path(node,path)
            if value is not None and _node_empty(value):
                _fail('missing_required_field',path,'required non-empty authored field')
        card = EmailCard(**{key:EMAIL_SPECS[key](value,key) for key,value in raw.items()})
        if kind == 'proposal':
            for path in ('counts','dispute_lane'):
                if path in raw:
                    _fail('kind_scope_violation',path,'record-scoped field on proposal')
        decision = parse_decision_block(body) if any(b.machine and b.info == 'decision' for b in blocks) else None
        return EmailCardEnvelope(card, decision, context, required_field_issues(card))
    except UnicodeError:
        return EmailCardRefusal('malformed_payload','body: valid Unicode required','body')
    except _EnvelopeInvalid as exc:
        return exc.refusal


def parse_email_card(body: str, *, own_card_id: str | None = None,
                     subject_permitted: bool = False) -> EmailCardEnvelope | EmailCardRefusal:
    """Strict pure convenience parse: §2–§5 + §8–§9; no ambient policy lookup.

    Consumers needing kind-specific named reasons call inspect_email_card and
    translate its required_issues/content/decision. Self-verification is checked
    when own_card_id is supplied; integrations must supply their known card id.
    """
    from hermes_cli.kanban_email_card import DecisionRefusal
    if type(subject_permitted) is not bool:
        return EmailCardRefusal('wrong_type','subject_permitted: explicit boolean required','subject_permitted')
    result = inspect_email_card(body)
    if isinstance(result,EmailCardRefusal):
        return result
    if result.content.issues:
        return result.content.issues[0].to_refusal()
    if result.required_issues:
        return result.required_issues[0]
    card = result.emailcard
    # required_issues has already proved these authored paths exist.
    assert card.verification is not None and card.action is not None and card.fences is not None
    if card.item is not None and card.item.subject is not None and not subject_permitted:
        return EmailCardRefusal('subject_not_permitted_by_board_policy','item.subject: disallowed by board policy','item.subject')
    if own_card_id is not None and card.verification.card_id == own_card_id:
        return EmailCardRefusal('self_verification','verification.card_id: independent card required','verification.card_id')
    missing = ACTION_FENCES[card.action] - set(card.fences)
    if missing:
        return EmailCardRefusal('missing_action_fence','fences: missing ' + ', '.join(sorted(missing)),'fences')
    if card.kind == 'proposal' and result.decision is None:
        return EmailCardRefusal('missing_decision_block','decision: proposal requires sibling container','decision')
    if isinstance(result.decision,DecisionRefusal):
        return EmailCardRefusal('malformed_decision_block',f'decision: {result.decision.code}','decision')
    if result.decision is not None and result.decision.card_kind != card.kind:
        return EmailCardRefusal('decision_kind_mismatch','decision.card_kind: differs from emailcard.kind','decision.card_kind')
    return result
