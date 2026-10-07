"""Frozen artifact-A tamper run; all writes target disposable board databases.

D2 precedence (manager Ruling 2, t_bcd6d68a) supersedes this card's original
unflagged-other-board refusal: elsewhere the guard is opt-in, not mandatory.
No integration marker: these offline cases run in the normal canonical suite.
"""
import builtins
import copy
import json
import socket
import subprocess
from pathlib import Path

import pytest
import yaml

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_email_card as ec
from tests.hermes_cli.test_kanban_board_content_policy import fresh_home

OWN = "t_76543210"
# Literal contract values, deliberately not derived from validator policy tables.
VALID_BODY = """Authored proposal rationale; no mail content.
```emailcard
kind: proposal
item:
  message_id: AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
action: draft_prepared
effect: Prepare an unsent draft
reversibility: reversible_gui
fences:
  - no_move
  - no_archive
  - no_filing
  - content_not_on_board
  - no_send
  - no_reply
  - no_forward
evidence:
  reason: Preparation rationale
  snapshot_hash: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
  thread_hash: not_reviewed
receipts:
  operations:
    - key: draft
      status: pending
verification:
  card_id: t_01234567
```
```decision
card_kind: proposal
revision: authored-r1
requires: needs_input
options:
  - id: A
    label: 'DECISION=A: prepare an unsent draft'
    effect: prepare only
    comment: 'DECISION=A: prepare an unsent draft; no sending authorized'
```
"""

# Each fixture tampers one property, while retaining the valid positive control.
TAMPER_CASES = (
    ("unknown_card_kind", VALID_BODY.replace("card_kind: proposal", "card_kind: alien"), "unknown_card_kind"),
    ("missing_required_field", VALID_BODY.replace("effect: Prepare an unsent draft\n", ""), "missing_required_field"),
    ("fence_not_asserted", VALID_BODY.replace("  - no_send\n", ""), "missing_action_fence"),
    ("reversibility_unnamed", VALID_BODY.replace("reversibility: reversible_gui\n", ""), "missing_required_field"),
    ("verification_not_separate", VALID_BODY.replace("card_id: t_01234567", "card_id: " + OWN), "self_verification"),
    ("message_body", VALID_BODY.replace("kind: proposal\n", "kind: proposal\nbody: forbidden fixture payload\n", 1), "forbidden_content_key"),
    ("draft_text", VALID_BODY.replace("kind: proposal\n", "kind: proposal\ndraft_body: forbidden fixture payload\n", 1), "forbidden_content_key"),
    ("extracted_document", VALID_BODY.replace("kind: proposal\n", "kind: proposal\ndocument: forbidden fixture payload\n", 1), "forbidden_content_key"),
    ("malformed_decision", VALID_BODY.replace("options:\n", "options: [\n"), "malformed_decision_block"),
    ("option_without_comment", VALID_BODY.replace("    comment: 'DECISION=A: prepare an unsent draft; no sending authorized'\n", ""), "malformed_decision_block"),
)


@pytest.fixture
def email_conn(fresh_home, monkeypatch):
    kb.create_board("sis-email")
    monkeypatch.setattr(kb, "_new_task_id", lambda: OWN)
    with kbc.connect(board="sis-email") as conn:
        yield conn


def rows(conn):
    return {table: tuple(tuple(row) for row in conn.execute("SELECT * FROM " + table))
            for table in ("tasks", "task_comments", "task_events")}


@pytest.mark.parametrize("name,text,code", TAMPER_CASES, ids=[case[0] for case in TAMPER_CASES])
def test_tamper_refused_with_reason_before_any_write(email_conn, name, text, code):
    kb.create_task(email_conn, title="Existing fixture", body="Existing authored body",
                   workspace_kind="scratch")
    before = rows(email_conn)
    changes = email_conn.total_changes
    # Real create path, not a mocked validator or 'does not crash' assertion.
    with pytest.raises(ValueError) as caught:
        kb.create_task(email_conn, title=name, body=text, workspace_kind="scratch")
    actual_code, reason = str(caught.value).split(": ", 1)
    assert actual_code == code
    assert reason.strip()
    if name in {"malformed_decision", "option_without_comment"}:
        parsed = ec.parse_decision_block(text)
        assert isinstance(parsed, ec.DecisionRefusal)
        assert parsed.code == {"malformed_decision": "malformed_payload",
                               "option_without_comment": "missing_option_comment"}[name]
        assert parsed.reason in reason
    assert rows(email_conn) == before
    assert email_conn.total_changes == changes


def test_exported_fixtures_match_exercised_cases():
    path = Path(__file__).resolve().parents[1] / "fixtures" / "email_card_tamper_run.json"
    exported = json.loads(path.read_text(encoding="utf-8"))
    assert exported == {
        "own_card_id": OWN,
        "positive_control": VALID_BODY,
        "tamper_cases": [{"name":name, "body":text, "refusal_code":code}
                         for name, text, code in TAMPER_CASES],
    }


def test_positive_control_accepted_unchanged(email_conn):
    parsed = ec._email_card_verdict(VALID_BODY, task_id=OWN, subject_permitted=False)
    assert isinstance(parsed, ec.EmailCardEnvelope)
    authored = yaml.safe_load(VALID_BODY.split("```emailcard\n", 1)[1].split("```", 1)[0])
    assert parsed.emailcard.to_dict() == authored
    assert ec.validate_email_card_body(email_conn, VALID_BODY, task_id=OWN) is None
    tid = kb.create_task(email_conn, title="Positive control", body=VALID_BODY, workspace_kind="scratch")
    assert kb.get_task(email_conn, tid).body.encode("utf-8") == VALID_BODY.encode("utf-8")


@pytest.mark.parametrize("opt_in", [False, True], ids=["D2_unflagged_pass", "explicit_flag_validates"])
def test_other_board_same_valid_card_accepted_unchanged(fresh_home, opt_in):
    kb.create_board("tamper-other")
    with kbc.connect(board="tamper-other") as conn:
        assert ec.validate_email_card_body(conn, VALID_BODY, task_id=OWN, opt_in=opt_in) is None
        tid = kb.create_task(conn, title="Same positive control", body=VALID_BODY,
                             workspace_kind="scratch")
        assert kb.get_task(conn, tid).body == VALID_BODY


def test_other_board_flag_engages_real_refusal(fresh_home):
    kb.create_board("tamper-other")
    text = TAMPER_CASES[0][1]
    with kbc.connect(board="tamper-other") as conn:
        before = rows(conn)
        with pytest.raises(ValueError, match="^unknown_card_kind: .+"):
            ec.validate_email_card_body(conn, text, task_id=OWN, opt_in=True)
        assert rows(conn) == before


@pytest.mark.parametrize("text", [VALID_BODY, TAMPER_CASES[0][1]], ids=["accepted", "refused"])
def test_deterministic_verdict_no_network_or_model(monkeypatch, text):
    # Warm imports before blocking all import attempts on the pure path. This
    # also catches an attempted lazy model/provider import, not just HTTP calls.
    import sys

    from unittest.mock import Mock
    ec._email_card_verdict(text, task_id=OWN, subject_permitted=False)
    deny = Mock(side_effect=AssertionError("network/model/subprocess access on validation path"))
    original_import = builtins.__import__
    def local_import(name, *args, **kwargs):
        if name.startswith(("openai", "anthropic", "run_agent", "agent", "providers", "litellm",
                            "httpx", "requests", "urllib", "http", "aiohttp")):
            return deny(name)
        if name not in sys.modules:
            return deny(name)
        return original_import(name, *args, **kwargs)
    with monkeypatch.context() as scope:
        for owner, attr in ((socket, "socket"), (socket, "create_connection"),
                            (socket, "getaddrinfo"), (subprocess, "Popen"),
                            (builtins, "open"), (Path, "open")):
            scope.setattr(owner, attr, deny)
        scope.setattr(builtins, "__import__", local_import)
        first = ec._email_card_verdict(text, task_id=OWN, subject_permitted=False)
        second = ec._email_card_verdict(text, task_id=OWN, subject_permitted=False)
    assert first == second
    assert isinstance(first, ec.EmailCardEnvelope if text == VALID_BODY else ec.DecisionRefusal)
    deny.assert_not_called()


# Independent literal required-path list: a future added default MUST fail even
# if someone also relaxes the validator's REQUIRED_PATHS table.
REQUIRED_PROPOSAL_PATHS = (
    "kind", "item.message_id", "action", "effect", "reversibility", "fences",
    "evidence.reason", "evidence.snapshot_hash", "evidence.thread_hash",
    "receipts.operations", "verification.card_id",
)


@pytest.mark.parametrize("path", REQUIRED_PROPOSAL_PATHS)
def test_missing_field_refused_never_defaulted(email_conn, path):
    payload = VALID_BODY.split("```emailcard\n", 1)[1].split("```", 1)[0]
    original = yaml.safe_load(payload)
    missing = copy.deepcopy(original)
    parent = missing
    parts = path.split(".")
    for part in parts[:-1]:
        parent = parent[part]
    del parent[parts[-1]]
    snapshot = copy.deepcopy(missing)
    text = VALID_BODY.replace(payload, yaml.safe_dump(missing, sort_keys=False))
    verdict = ec._email_card_verdict(text, task_id=OWN, subject_permitted=False)
    assert isinstance(verdict, ec.EmailCardRefusal), (path, verdict)
    assert verdict.code and verdict.reason.strip()
    assert path in verdict.reason or path == "kind" and verdict.code == "missing_card_kind"
    assert missing == snapshot
    with pytest.raises(ValueError, match="^" + verdict.code + ": .+"):
        ec.validate_email_card_body(email_conn, text, task_id=OWN)
    assert rows(email_conn)["tasks"] == ()
