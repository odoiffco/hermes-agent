# Email-card shared write boundary

`hermes_cli.kanban_email_card.validate_email_card_body(conn, body, *, task_id,
board=None, opt_in=False) -> None` is the single public write-boundary callable.
CLI `kanban_command` / `_cmd_create` and the decorated `kanban_create` handler
both call `kanban_db.create_task`; that function invokes the guard after each
new own-card ID is generated and before opening its write transaction. Accepted
body bytes are persisted unchanged. A refusal raises `ValueError` with a coded
reason: CLI stderr `kanban: <reason>` / rc 1; tool JSON error
`kanban_create: <reason>`. The D3 correction §3.5 explicitly governs over the
original implementation card's parsed-return/two-direct-call-sites paraphrase.

The adapter snapshots the root and actual connection file once (approved D3
R1–R5, t_c3770fe1 attachment 361), resolves one slug, and reads the reviewed
`board_subject_permitted` policy reader. Caller `board` metadata never chooses
the verdict. The private verdict composition uses only body, own-card ID and
subject-policy bool; it has no environment, filesystem, network, model or clock
access. Adapter filesystem/environment reads are intentional and are not a
claim that the entire public adapter is pure.

Engagement is memo D2: machine containers on connection-resolved `sis-email`,
or explicit opt-in, engage. Blockless bodies pass on all boards. The shared
container scanner distinguishes nested example text from real machine blocks;
malformed machine fences still engage and refuse. Opted-in unresolved
connections refuse `board_unresolvable`; there is no default-board fallback.

The composition consumes the reviewed envelope parser and both kind validators,
not a second field validator. Shared content findings (R3–R6) refuse before
kind dispatch. Per-field validator refusals pass through unchanged. Unknown
and missing kinds have distinct refusals; the decision parser's previous generic
missing-field code is refined to `missing_card_kind` for that field only. Normal
acceptance returns the original envelope internally and None publicly. An
independent no-fill postcondition rejects replacement envelopes, altered authored
email/decision fields, or a purported acceptance with missing required fields;
it reuses the parser's required-path helper, not a duplicated matrix.

Limits / subsequent work:

- This is a side-ref artifact, not enabled on served main or deployed.
- Opt-in flag/schema/kwarg plumbing belongs to t_0080ff9f. The create_task hook
  here uses the default; the public adapter already supports explicit opt-in.
- Decomposer, specify, edit and dashboard body rewrites are not covered by this
  create hook; t_23704508 owns the remaining write paths.
- Idempotent duplicate create returns the existing ID before validation and
  writes no submitted body; the focused test checks preservation explicitly.
- No prose-paraphrase detection is claimed. Producer gaps F0/F1/F4/F5 from the
  wire grammar §9 remain rollout prerequisites, not policy exemptions.

Focused acceptance (isolated homes, real SQLite and CLI/tool exception paths):

    .venv/bin/python -m pytest tests/hermes_cli/test_kanban_email_entrypoint.py tests/hermes_cli/test_kanban_email_proposal.py tests/hermes_cli/test_kanban_email_record.py tests/hermes_cli/test_kanban_emailcard_envelope.py tests/hermes_cli/test_kanban_decision_parser.py tests/hermes_cli/test_kanban_board_content_policy.py -o addopts= -q

Regression comparison on assembled base and candidate:

    .venv/bin/python -m pytest tests/hermes_cli/test_kanban_db.py tests/hermes_cli/test_kanban_core_functionality.py tests/hermes_cli/test_kanban_boards.py tests/tools/test_kanban_tools.py -o addopts= -q --tb=short

Sibling-authored eight-case exercise receipts remain t_9c6c9d0e's deliverable;
these implementation tests do not claim to replace that card's fixtures.
