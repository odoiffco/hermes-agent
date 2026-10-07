# Emailcard envelope API

Pure parsing for the frozen email-card wire grammar v1.2. Public imports are from
`hermes_cli.kanban_email_card`; implementation is in `kanban_email_envelope.py`.
The existing `parse_decision_block` and decision dataclasses are unchanged.
There is no board resolution, environment, clock, network or mailbox access.

## Entry points

- `inspect_email_card(body: str) -> EmailCardEnvelope | EmailCardRefusal` parses
  the closed shape/types and supplies `required_issues` and `content.issues` for
  kind-specific validators. Unknown keys, invalid types/bounds, absent kind,
  empty mandatory paths and forbidden content keys return a refusal immediately.
  Missing required paths other than kind remain absent in the typed representation
  and are enumerated individually in `required_issues`. No missing value is filled.
- `parse_email_card(body: str, *, own_card_id: str | None = None,
  subject_permitted: bool = False) -> EmailCardEnvelope | EmailCardRefusal` is the
  strict convenience entry point. It additionally enforces required paths,
  content findings, subject policy, action fences, self-verification when the
  caller supplies its card id, and decision presence/validity/kind agreement.
- `scan_email_content(body: str) -> ContentCheckContext | EmailCardRefusal` exposes
  R3–R6 independently. Validators do not parse prose or YAML again.
- `required_paths(card: EmailCard) -> tuple[str, ...]` supplies the per-kind matrix
  plus the acting-record condition; `required_field_issues(card)` names each
  missing/empty path.

Integrations must supply their actual known card id for self-verification and
board-resolved subject policy. An inspection result is not an acceptance verdict.
A kind validator translates shared parse/required/content refusals into its own
reason namespace; it must also check subject policy, self-verification, fences
and its decision requirements using the typed values. Do not default board policy
from ambient environment. Board resolution is a separate upstream concern.

## Typed representation and authored presence

`EmailCardEnvelope(emailcard, decision, content, required_issues)` is immutable.
`emailcard` is `EmailCard(kind, item, action, effect, reversibility, fences,
evidence, receipts, verification, dispute_lane, counts)` with nested frozen
`EmailItem`, `EmailEvidence`, `EmailReceipts`, `EmailVerification`, and
`EmailOperation(key, status)` dataclasses. Optional fields use `None` for absence;
authored YAML null is not accepted as a value. `.to_dict()` recursively exports
only authored fields; an authored empty optional map/list/string remains empty.
`get_path('evidence.thread_hash')` returns a typed value or `None` for absence.
Strings retain their semantic YAML bytes, including multiline scalars; ids and
hashes remain strings, integers are typed decimal integers, maps are read-only,
and lists become tuples. Counter vectors retain their authored pipe string;
`counters_before_vector` / `counters_after_vector` expose integer tuples in the
`COUNTER_FIELDS` order without replacing the authored value. Dates remain their
validated authored strings. Decision `expires` is never added when absent.

## Required-path data (frozen §9)

`REQUIRED_PATHS['record']`:

    kind, action, effect, fences, evidence.reason, evidence.thread_hash,
    receipts.counters_after, verification.card_id, dispute_lane, counts

`REQUIRED_PATHS['proposal']`:

    kind, item.message_id, action, effect, reversibility, fences,
    evidence.reason, evidence.snapshot_hash, evidence.thread_hash,
    receipts.operations, verification.card_id

Proposals also require a sibling valid decision block; this is a container
requirement rather than an emailcard path. `counts` and `dispute_lane` are refused
on proposals. Subject is never required and is default-disallowed.

`ACTING_COUNT_KEYS` is `acted, tagged_review, tagged_action, tags_cleared,
marked_read, drafts_prepared`. Any of these >=1 adds mandatory nonempty
`receipts.operations` on records. `passes_in_window >=1` alone does not.
Mandatory empty string/list/map is refused; no count or receipt is synthesized.

`RECORD_GROUP_PATHS` supplies semantic-group mapping:

| Group | Paths |
| --- | --- |
| counts | counts |
| acts_applied | action, counts, receipts.operations |
| evidence_basis | evidence.reason, evidence.thread_hash |
| receipts | receipts.counters_after, receipts.operations |
| dispute_lane | dispute_lane |

Record action is the card's own act, not the pass's: filer-filed records author
`no_action`. Pass acts live in counts and conditional operation receipts.
`evidence.thread_hash` is always required; `not_reviewed` is an authored literal,
not a parser default. Record `snapshot_hash` and every `item.*` path are optional.
Proposal item reference is exactly required `item.message_id`, not mandatory
sender/subject/folder. Proposal hashes and operation receipt are mandatory.
Record reversibility is optional; proposal reversibility is mandatory.

## Every allowed path

The schema is closed at every map and operation entry. Allowed item fields:
message_id/conversation_id (20–400 identifier characters), folder (inbox,
unknown, other:name up to 64), received_date (ISO calendar date or zoned
date-time Z/+08:00), lowercase DNS sender_domain (<=253), optional sender_display
(<=64 name characters), operator-gated subject (<=120, no newline).

Root action/reversibility are closed enums; effect <=200 without newline;
fences use `FENCE_KEYS` and `ACTION_FENCES` including the standing set.
Evidence: reason <=2000; snapshot_hash lowercase hex64; thread_hash hex64 or
not_reviewed. Receipts: counters_before/counters_after nine pipe-separated
integers; operations <=8 {key <=120, status verified/pending/refused};
attention_readback <=8 integer entries using Review/Action/null (literal null
key distinct from absence). Verification: card_id t_ + eight lowercase hex,
optional kind literal independent. Record dispute_lane <=400; counts <=12
integer entries using `COUNT_KEYS`. Payload <=8192 UTF-8 bytes, each line <=200
characters, keys <=32. Duplicates, aliases, anchors, merges, custom types,
multiple/truncated containers and unrecognized values are refused.

## Content context

`ContentCheckContext.issues` carries deterministic `ContentIssue(code, path,
line, length)` values, with no copied content. `issue.to_refusal()` gives a
shared `EmailCardRefusal(code, reason, path)`. Codes are
`forbidden_content_key`, `base64_content_run`, `oversized_prose_fence`, and
`overlong_quoted_line` for R3–R6. Forbidden keys are scanned at arbitrary nested
locations in both machine containers, including unknown maps. Long encoded
runs are checked both in semantic YAML scalars and raw text/comments. Only
`BASE64_PERMITTED_PATHS` (item identifiers and evidence hashes) are exempt;
rationale, subject, decision comments and prose are not encoded-content escapes.
Non-machine fence payload >400 characters and prose quote lines >=200 are
reported. Parser bounds remain enforced on both machine containers.

This does not detect paraphrased mail content in ordinary prose. Truthfulness of
operation statuses and counts is not a parsing guarantee. Producer gaps
F0/F1/F4/F5 and filer-first rollout ordering remain unchanged; this API does not
enable validation on a board or change any producer.
