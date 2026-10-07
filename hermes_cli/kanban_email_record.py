"""Pure record verdict over the shared parser's typed result; no parsing or I/O.

Accepted results are the exact input envelope, not a repaired copy. Callers must
supply the card's own id and a resolved subject-policy boolean. Board resolution,
producer upgrades, and write hooks deliberately live outside this module.
"""
from __future__ import annotations

import re

from hermes_cli.kanban_email_card import DecisionRefusal
from hermes_cli.kanban_email_envelope import (
    ACTION_FENCES, EmailCardEnvelope, EmailCardRefusal, required_field_issues,
)


def _refusal(code: str, path: str, detail: str) -> EmailCardRefusal:
    return EmailCardRefusal('record_' + code, f'{path}: {detail}', path)


def _translate(issue: EmailCardRefusal) -> EmailCardRefusal:
    if issue.code == 'missing_required_field':
        return _refusal('missing_' + issue.path, issue.path,
                        'required non-empty authored record field')
    return EmailCardRefusal('record_' + issue.code, issue.reason, issue.path)


def validate_record(
    parsed: EmailCardEnvelope | EmailCardRefusal, *, own_card_id: str,
    subject_permitted: bool,
) -> EmailCardEnvelope | EmailCardRefusal:
    """Refuse never fill, using only parser findings and explicit caller context.

    Use inspect_email_card before calling. A parse refusal is translated into the
    record namespace, including an exact-path missing-field code. A valid record
    is returned by identity. Absent expires stays absent; authored expires is
    preserved verbatim, never compared against a clock. Optional decision blocks
    must still be valid and agree with emailcard.kind.

    The parser is the trusted type/bounds boundary, not arbitrary hand-built
    dataclasses. Content checks are the frozen R3-R6 structural guarantees, not
    a detector for paraphrased message content in ordinary prose.
    """
    if isinstance(parsed, EmailCardRefusal):
        return _translate(parsed)
    if not isinstance(parsed, EmailCardEnvelope):
        return _refusal('invalid_input', 'parsed', 'typed parser result required')
    card = parsed.emailcard
    if card.kind != 'record':
        return _refusal('kind_mismatch', 'kind', 'record required')
    if type(own_card_id) is not str or re.fullmatch(r't_[0-9a-f]{8}', own_card_id) is None:
        return _refusal('invalid_own_card_id', 'own_card_id', 'explicit card id required')
    if type(subject_permitted) is not bool:
        return _refusal('invalid_subject_policy', 'subject_permitted', 'explicit boolean required')
    if parsed.content.issues:
        return _translate(parsed.content.issues[0].to_refusal())
    # Re-evaluate paths from typed authored values, not a cached verdict.
    issues = required_field_issues(card)
    if issues:
        return _translate(issues[0])
    # Required-path checks above prove these values exist.
    assert card.verification is not None and card.action is not None and card.fences is not None
    if card.item is not None and card.item.subject is not None and not subject_permitted:
        return _refusal('subject_not_permitted_by_board_policy', 'item.subject',
                        'disallowed by board policy (R9)')
    if card.verification.card_id == own_card_id:
        return _refusal('self_verification', 'verification.card_id',
                        'independent card required (R8)')
    fences = ACTION_FENCES.get(card.action)
    if fences is None:
        return _refusal('unknown_action_fences', 'action', 'known fence requirements required (R7)')
    missing = fences - set(card.fences)
    if missing:
        return _refusal('missing_action_fence', 'fences', 'missing ' + ', '.join(sorted(missing)) + ' (R7)')
    if isinstance(parsed.decision, DecisionRefusal):
        return _refusal('malformed_decision_block', 'decision', parsed.decision.code + ' (R10)')
    if parsed.decision is not None and parsed.decision.card_kind != 'record':
        return _refusal('decision_kind_mismatch', 'decision.card_kind', 'record required (R10)')
    return parsed
