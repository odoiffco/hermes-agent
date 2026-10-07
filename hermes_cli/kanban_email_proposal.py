"""Pure proposal verdicts on the shared parser's typed output.

Shape/type/bound and R3 checks belong to inspect_email_card, not a second
parser here. Acceptance returns the identical envelope, never a filled copy.
"""
from __future__ import annotations

import re

from hermes_cli.kanban_email_envelope import (
    ACTION_FENCES, EmailCardEnvelope, EmailCardRefusal, required_field_issues,
)


def validate_proposal(
    parsed: EmailCardEnvelope | EmailCardRefusal, *, own_card_id: str | None,
    subject_permitted: bool = False,
) -> EmailCardEnvelope | EmailCardRefusal:
    """Validate inspect_email_card output using explicit identity and policy.

    The caller must supply a valid own-card id; None is refused, not a bypass.
    No board lookup, parsing, expiry evaluation or model/content inference occurs.
    Refusals from inspection pass through unchanged, preserving exact paths.
    Hand-constructed envelopes are not a substitute for the shared parser.
    """
    from hermes_cli.kanban_email_card import DecisionRefusal

    if isinstance(parsed, EmailCardRefusal):
        return parsed
    if not isinstance(parsed, EmailCardEnvelope):
        return EmailCardRefusal('wrong_type', 'emailcard: parsed envelope required', 'emailcard')
    card = parsed.emailcard
    if card.kind != 'proposal':
        return EmailCardRefusal('wrong_card_kind', 'kind: proposal required', 'kind')
    if parsed.content.issues:
        return parsed.content.issues[0].to_refusal()
    # Check actual authored values, not just the cached missing-path findings.
    issues = required_field_issues(card)
    if issues:
        return issues[0]
    assert card.item is not None and card.verification is not None
    assert card.action is not None and card.fences is not None
    if type(subject_permitted) is not bool:
        return EmailCardRefusal('wrong_type', 'subject_permitted: explicit boolean required', 'subject_permitted')
    if not isinstance(own_card_id, str) or re.fullmatch(r't_[0-9a-f]{8}', own_card_id) is None:
        return EmailCardRefusal('invalid_own_card_id', 'own_card_id: explicit t_ plus eight lowercase hex digits required', 'own_card_id')
    if card.item.subject is not None and not subject_permitted:
        return EmailCardRefusal('subject_not_permitted_by_board_policy', 'item.subject: disallowed by board policy', 'item.subject')
    if card.verification.card_id == own_card_id:
        return EmailCardRefusal('self_verification', 'verification.card_id: separate independent card required', 'verification.card_id')
    missing = ACTION_FENCES[card.action] - set(card.fences)
    if missing:
        return EmailCardRefusal('missing_action_fence', 'fences: missing ' + ', '.join(sorted(missing)), 'fences')
    if parsed.decision is None:
        return EmailCardRefusal('missing_decision_block', 'decision: proposal requires sibling container', 'decision')
    if isinstance(parsed.decision, DecisionRefusal):
        return EmailCardRefusal('malformed_decision_block', f'decision: {parsed.decision.code}; {parsed.decision.reason}', 'decision')
    if parsed.decision.card_kind != card.kind:
        return EmailCardRefusal('decision_kind_mismatch', 'decision.card_kind: differs from emailcard.kind', 'decision.card_kind')
    return parsed
