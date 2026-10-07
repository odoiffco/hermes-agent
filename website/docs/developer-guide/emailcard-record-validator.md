# Record validator (not enabled on any board)

`hermes_cli.kanban_email_record.validate_record(parsed, *, own_card_id,
subject_permitted)` consumes the approved shared parser's
`EmailCardEnvelope | EmailCardRefusal`. Obtain that result with
`inspect_email_card(body)`; this branch never reads or parses the body itself.
The caller supplies the new card's id and a resolved boolean subject policy.
No board resolver, environment, filesystem, model, network or clock is read.

Acceptance returns the exact envelope object, unchanged. Refusals use the
shared `EmailCardRefusal` type with `record_`-prefixed codes. Missing or empty
paths get distinct `record_missing_<exact.path>` codes and exact-path reasons;
parser type/bound errors retain their original reason and path under the record
namespace. These codes are separate from proposal codes. Required paths and
the conditional acting-count receipt trigger come from the envelope's frozen
v1.2 tables, not a second schema. A cached required-issue list cannot suppress
the typed required-path check.

Record groups: counts; acts applied (action, counts, conditional operations);
evidence basis (reason and thread_hash); receipts (counters_after and conditional
operations); dispute_lane. A record's no_action does not erase pass-level acting
counts. Missing/empty operations refuses when any acting count is at least one;
non-acting records may omit operations or author an empty list. Optional
snapshot_hash, item metadata, reversibility and before-counters stay optional.
The validator inserts nothing, including thread_hash and expires.

All parser content findings (R3-R6), action fence requirements (R7), independent
verification (R8), subject policy (R9), and errors in an optional present decision
block (R10) refuse. Authored expires is preserved as the parser's string; this
branch neither reinterprets it nor decides expiry. UI expiry is outside this
pure branch. Known content containers are refused; prose paraphrases are not
mechanically detectable and no guarantee about them is claimed.

No production call site is installed. Frozen producer gaps F0/F1/F4/F5 must be
closed before enabling validation for filer-created records. The shared-entrypoint
card owns integration and board resolution, not this module.
