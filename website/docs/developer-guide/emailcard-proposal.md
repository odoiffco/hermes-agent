# Proposal verdict API

`hermes_cli.kanban_email_proposal.validate_proposal(parsed, *, own_card_id,
subject_permitted=False)` consumes the output of the shared
`hermes_cli.kanban_email_card.inspect_email_card(body)` parser. It does not
parse YAML, scan prose, resolve boards or call models. Pass an inspection refusal
as-is; the validator preserves it. Hand-constructed envelopes are not supported
as a way to skip shared shape/type/bound/content inspection.

A refusal is `EmailCardRefusal(code, reason, path)`. Acceptance is the identical
`EmailCardEnvelope` object supplied by the caller, not a repaired or filled copy.
Missing or empty authored fields receive exact-path reasons. The proposal floor
is the shared `REQUIRED_PATHS['proposal']` from frozen wire grammar v1.2 §9.3:
kind, item.message_id, action, effect, reversibility, fences, evidence.reason,
evidence.snapshot_hash, evidence.thread_hash, receipts.operations and
verification.card_id. Metadata beyond message_id remains optional. The sibling
decision block must parse successfully and agree on proposal kind.

Supply the card's own `t_` plus eight lowercase hex id explicitly. None, malformed
identity and self-verification are refused. This branch does not look up the
verifier's existence or status; that is not part of the frozen identifier rule.
Callers creating a card must pin its prospective id before checking independence.
Subject policy must be a bool: subject is refused by default, including an
explicitly authored empty subject. A true argument permits bounded subject
metadata; no policy is discovered from environment or filesystem here.

Known message/draft/document containers are refused by the shared parser's
R3–R6 context and forwarded by the verdict. This is not a guarantee that arbitrary
ordinary prose or a rationale contains no paraphrase of mail: frozen grammar §6
explicitly leaves that to review. Receipt statuses and rationale are authored
claims; this pure validator cannot verify their external truth. Reversibility is
named by the closed enum, not inferred or defaulted from action. Optional decision
expiry is preserved, not evaluated against a clock by this pure branch.

No board/tool/CLI wiring or rollout is included. Integration callers import the
topical module directly; this change leaves the shared decision-parser facade
untouched to avoid parallel validator export conflicts.
