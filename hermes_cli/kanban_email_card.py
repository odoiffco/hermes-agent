"""Pure decision-block parsing. Board resolution and email validation live outside it.

YAML nodes are inspected, not constructed: no implicit scalar coercion, aliases,
merge keys, custom tags or duplicate keys can supply author-absent values.
"""
from dataclasses import dataclass
import re
from typing import NoReturn

import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode
from yaml.tokens import AliasToken, AnchorToken


@dataclass(frozen=True)
class DecisionRefusal:
    code: str
    reason: str


@dataclass(frozen=True)
class DecisionOption:
    id: str
    label: str
    effect: str
    comment: str

    def to_dict(self) -> dict[str, str]:
        return {key: getattr(self, key) for key in ("id", "label", "effect", "comment")}


@dataclass(frozen=True)
class DecisionBlock:
    card_kind: str
    revision: str
    requires: str
    options: tuple[DecisionOption, ...]

    def to_dict(self) -> dict:
        return {"card_kind": self.card_kind, "revision": self.revision,
                "requires": self.requires, "options": [o.to_dict() for o in self.options]}


@dataclass(frozen=True)
class ExpiringDecisionBlock(DecisionBlock):
    expires: str

    def to_dict(self) -> dict:
        return {**super().to_dict(), "expires": self.expires}


class _Invalid(Exception):
    def __init__(self, code: str, reason: str):
        self.refusal = DecisionRefusal(code, reason)


def _refuse(code: str, reason: str) -> NoReturn:
    raise _Invalid(code, reason)


def _mapping(node, allowed: set[str], required: set[str], path: str):
    if not isinstance(node, MappingNode) or node.tag != "tag:yaml.org,2002:map":
        _refuse("wrong_type", f"Decision schema requires a map at {path}.")
    values = {}
    for key, value in node.value:
        if not isinstance(key, ScalarNode) or key.tag != "tag:yaml.org,2002:str":
            _refuse("wrong_type", f"Decision schema requires string keys at {path}.")
        if key.value in values:
            _refuse("duplicate_key", f"Decision schema repeats a key at {path}.")
        if key.value not in allowed:
            _refuse("unknown_key", f"Decision schema has an unknown key at {path}.")
        values[key.value] = value
    if "card_kind" in required and "card_kind" not in values:
        _refuse("missing_card_kind", "Decision schema requires an authored card_kind.")
    if "comment" in required and "comment" not in values:
        _refuse("missing_option_comment", "Decision option requires a non-empty comment.")
    if required - values.keys():
        _refuse("missing_field", f"Decision schema is missing a required field at {path}.")
    return values


def _string(node, path: str, *, timestamp: bool = False):
    tags = {"tag:yaml.org,2002:str"}
    if timestamp:
        tags.add("tag:yaml.org,2002:timestamp")
    if not isinstance(node, ScalarNode) or node.tag not in tags:
        _refuse("wrong_type", f"Decision schema requires a string at {path}.")
    return node.value


def _payload(body: str) -> str:
    # Track all fences so a decision-looking line inside another code block
    # cannot become an authority container. Only exact triple-backtick decision
    # info strings count; Markdown indentation up to three spaces is allowed.
    active = None
    decision = False
    found = []
    lines = []
    for line in body.splitlines(keepends=True):
        fence = re.fullmatch(r" {0,3}(`{3,}|~{3,})([^\r\n]*)\r?\n?", line)
        if active is None:
            if fence:
                marker, info = fence.groups()
                active = marker
                decision = marker == "```" and info.strip() == "decision"
                if info.strip().startswith("decision") and not decision:
                    _refuse("bad_fence", "Decision block requires an exact triple-backtick decision fence.")
                lines = []
        elif fence and fence.group(1)[0] == active[0] and len(fence.group(1)) >= len(active) and not fence.group(2).strip():
            if decision:
                if fence.group(1) != "```":
                    _refuse("bad_fence", "Decision block requires an exact triple-backtick closing fence.")
                found.append("".join(lines))
            active = None
            decision = False
        elif decision:
            lines.append(line)
    if decision:
        _refuse("truncated_block", "Decision block has no closing fence.")
    if not found:
        _refuse("missing_fence", "Card body has no fenced decision block.")
    if len(found) != 1:
        _refuse("multiple_blocks", "Card body must contain exactly one decision block.")
    return found[0]


def parse_decision_block(body: str) -> DecisionBlock | DecisionRefusal:
    """Return one complete immutable block or a deterministic coded refusal.

    Optional expires is represented only by ExpiringDecisionBlock; it is never
    added to a non-expiring block. Scalar values retain YAML string semantics
    (including block-scalar newlines), without stripping or backfilling.
    """
    try:
        if not isinstance(body, str):
            _refuse("wrong_type", "Card body must be a string.")
        payload = _payload(body)
        try:
            payload_size = len(payload.encode("utf-8"))
        except UnicodeError:
            _refuse("malformed_payload", "Decision block must contain valid Unicode text.")
        if payload_size > 8192 or any(len(line) > 200 for line in payload.splitlines()):
            _refuse("out_of_bounds", "Decision block exceeds the 8 KiB or 200-character line bound.")
        try:
            if any(isinstance(token, (AliasToken, AnchorToken)) for token in yaml.scan(payload)):
                _refuse("yaml_reference", "Decision block must not use YAML anchors or aliases.")
            node = yaml.compose(payload, Loader=yaml.SafeLoader)
        except (yaml.YAMLError, RecursionError):
            _refuse("malformed_payload", "Decision block is not a complete YAML document.")
        fields = _mapping(node, {"card_kind", "revision", "requires", "expires", "options"},
                          {"card_kind", "revision", "requires", "options"}, "decision")
        strings = {key: _string(fields[key], key) for key in ("card_kind", "revision", "requires")}
        if strings["card_kind"] not in {"proposal", "record"}:
            _refuse("unknown_card_kind", "Decision card_kind must be proposal or record.")
        options_node = fields["options"]
        if not isinstance(options_node, SequenceNode) or options_node.tag != "tag:yaml.org,2002:seq":
            _refuse("wrong_type", "Decision options must be a list.")
        if not options_node.value:
            _refuse("empty_options", "Decision options must not be empty.")
        options = []
        ids = set()
        for option in options_node.value:
            values = _mapping(option, {"id", "label", "effect", "comment"},
                              {"id", "label", "effect", "comment"}, "options")
            if isinstance(values["comment"], ScalarNode) and not values["comment"].value:
                _refuse("empty_option_comment", "Decision option comment must not be empty.")
            values = {key: _string(value, f"options.{key}") for key, value in values.items()}
            if not values["comment"].strip():
                _refuse("empty_option_comment", "Decision option comment must not be empty.")
            if not values["id"]:
                _refuse("empty_option_id", "Decision option id must not be empty.")
            if values["id"] in ids:
                _refuse("duplicate_option_id", "Decision option ids must be unique.")
            ids.add(values["id"])
            options.append(DecisionOption(**values))
        if "expires" in fields:
            return ExpiringDecisionBlock(**strings, options=tuple(options),
                                         expires=_string(fields["expires"], "expires", timestamp=True))
        return DecisionBlock(**strings, options=tuple(options))
    except _Invalid as exc:
        return exc.refusal


# Additive public envelope API; the established decision API above is unchanged.
from hermes_cli.kanban_email_envelope import (
    ACTION_FENCES, ACTING_COUNT_KEYS, ATTENTION_KEYS, BASE64_PERMITTED_PATHS,
    COUNTER_FIELDS, COUNT_KEYS, FENCE_KEYS, FORBIDDEN_CONTENT_KEYS,
    RECORD_GROUP_PATHS, REQUIRED_PATHS, STANDING_FENCES,
    ContentCheckContext, ContentIssue, EmailCard, EmailCardEnvelope,
    EmailCardRefusal, EmailEvidence, EmailItem, EmailOperation, EmailReceipts,
    EmailVerification, inspect_email_card, parse_email_card,
    required_field_issues, required_paths, scan_email_content,
)


def _email_card_verdict(body, *, task_id, subject_permitted):
    """Pure composition: shared content gate, kind dispatch, no-fill postcondition.

    The parser and validators own all field rules. Their refusals are returned
    unchanged. Acceptance must be the same authored envelope, without additions
    or alterations by either branch; no replacement/defaulted result can pass.
    """
    from hermes_cli.kanban_email_proposal import validate_proposal
    from hermes_cli.kanban_email_record import validate_record

    parsed = inspect_email_card(body)
    if isinstance(parsed, EmailCardRefusal):
        if parsed.path == 'kind' and parsed.code == 'out_of_bounds':
            return EmailCardRefusal('unknown_card_kind', 'kind: record or proposal required', 'kind')
        if parsed.path == 'kind' and parsed.code == 'missing_required_field':
            return EmailCardRefusal('missing_card_kind', 'kind: authored card kind required', 'kind')
        return parsed
    if parsed.content.issues:
        return parsed.content.issues[0].to_refusal()
    if isinstance(parsed.decision, DecisionRefusal) and parsed.decision.code in {'unknown_card_kind', 'missing_card_kind'}:
        return parsed.decision
    validator = {'proposal': validate_proposal, 'record': validate_record}.get(parsed.emailcard.kind)
    if validator is None:
        return EmailCardRefusal('unknown_card_kind', 'kind: record or proposal required', 'kind')
    authored = parsed.emailcard.to_dict()
    decision = parsed.decision.to_dict() if isinstance(parsed.decision, DecisionBlock) else parsed.decision
    result = validator(parsed, own_card_id=task_id, subject_permitted=subject_permitted)
    if isinstance(result, EmailCardRefusal):
        return result
    if (result is not parsed or required_field_issues(parsed.emailcard) or
            parsed.emailcard.to_dict() != authored or
            (parsed.decision.to_dict() if isinstance(parsed.decision, DecisionBlock) else parsed.decision) != decision):
        return EmailCardRefusal('refuse_never_fill', 'emailcard: validator must preserve authored fields unchanged', 'emailcard')
    return result


def validate_email_card_body(conn, body, *, task_id, board=None, opt_in=False) -> None:
    """D1 write-boundary entrypoint shared by CLI and tools through create_task.

    D2: blockless bodies pass everywhere; machine blocks engage on sis-email or
    explicit opt-in. Caller board intent never selects policy. D3's adapter is
    the sole ambient read boundary; the verdict composition itself is pure.
    On acceptance return None, never a rewritten body; refusals raise ValueError
    with one coded reason for the existing CLI/tool exception surfaces.
    """
    from hermes_cli.kanban_email_board import board_snapshot, resolve_board_slug
    from hermes_cli.kanban_db import board_subject_permitted

    from hermes_cli.kanban_email_envelope import _containers, _EnvelopeInvalid

    slug = resolve_board_slug(*board_snapshot(conn))
    if not isinstance(body, str):
        return None
    try:
        blocks, _prose = _containers(body)
        has_block = any(block.machine for block in blocks)
    except _EnvelopeInvalid as exc:
        # A malformed machine fence must engage and receive the parser's reason,
        # not pass through because it failed to produce a complete container.
        has_block = exc.refusal.code in {'bad_fence', 'truncated_block'}
    if not has_block:
        return None
    if slug != 'sis-email' and not opt_in:
        return None
    if slug is None:
        raise ValueError('board_unresolvable: opened connection has no canonical board identity')
    subject_ok = board_subject_permitted(slug)
    result = _email_card_verdict(body, task_id=task_id, subject_permitted=subject_ok)
    if isinstance(result, (EmailCardRefusal, DecisionRefusal)):
        raise ValueError(f'{result.code}: {result.reason}')
    return None
