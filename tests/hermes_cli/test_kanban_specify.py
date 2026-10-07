"""Tests for the specifier module + `hermes kanban specify` CLI surface.

The auxiliary LLM client is mocked — these tests don't hit any network or
real provider. They exercise the prompt plumbing, response parsing, DB
writes, and CLI flag surface.
"""

from __future__ import annotations

import argparse
import json as jsonlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_specify as spec

ACCEPTANCE = ("\n**Acceptance criteria**\nObservable result: A local receipt lists the checked result."
              "\nFalsifier: The local receipt is absent or lists a failed check.")


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _fake_aux_response(content: str):
    """Build a minimal object shaped like an OpenAI chat.completions result.

    The specifier only reads ``resp.choices[0].message.content``, so we
    avoid importing the openai SDK and build the tree with MagicMock.
    """
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    return resp


def _mock_client_returning(content: str):
    client = MagicMock()
    client.chat.completions.create = MagicMock(return_value=_fake_aux_response(content))
    return client


def _patch_aux_client(content: str, *, model: str = "test-model"):
    """Patch call_llm at its source module — specify_task now routes through
    it (#35566) instead of building a raw client. Returns (patcher, mock) so
    callers can still assert on the call.
    """
    mock_fn = MagicMock(return_value=_fake_aux_response(content))
    return patch("agent.auxiliary_client.call_llm", mock_fn), mock_fn


# ---------------------------------------------------------------------------
# JSON extraction helpers
# ---------------------------------------------------------------------------





# ---------------------------------------------------------------------------
# specify_task (module-level entry point)
# ---------------------------------------------------------------------------

def test_specify_task_happy_path(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="rough", triage=True)

    content = jsonlib.dumps({
        "title": "Refined rough",
        "body": "**Goal**\nA concrete goal." + ACCEPTANCE,
    })
    p, _ = _patch_aux_client(content)
    with p:
        outcome = spec.specify_task(tid, author="ace")

    assert outcome.ok is True
    assert outcome.task_id == tid
    assert outcome.new_title == "Refined rough"

    with kbc.connect() as conn:
        task = kb.get_task(conn, tid)
    # Parent-free → recompute_ready promotes to ready.
    assert task.status == "ready"
    assert task.title == "Refined rough"
    assert "**Goal**" in (task.body or "")
    assert task is not None
    assert spec._acceptance_problem(task.body) == ""


def test_specify_refuses_unobservable_acceptance(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="Investigate a report", triage=True)
    content = jsonlib.dumps({"title": "Investigate", "body":
        "**Goal**\nInvestigate.\n**Acceptance criteria**\n- Make it work."})
    p, _ = _patch_aux_client(content)
    with p:
        outcome = spec.specify_task(tid, author="ace")
    assert not outcome.ok
    assert "acceptance" in outcome.reason.lower()
    with kbc.connect() as conn:
        task = kb.get_task(conn, tid)
        assert task is not None and task.status == "triage"


def test_specify_honors_explicit_refusal(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="Decide later", triage=True)
    p, _ = _patch_aux_client(jsonlib.dumps({"refused": True, "reason": "No source specified"}))
    with p:
        outcome = spec.specify_task(tid, author="ace")
    assert not outcome.ok and "No source specified" in outcome.reason
    with kbc.connect() as conn:
        task = kb.get_task(conn, tid)
        assert task is not None and task.status == "triage"


def test_research_citation_check_is_an_observable_falsifier():
    body = ("**Acceptance criteria**\nObservable result: Three claims cite retrievable sources."
            "\nFalsifier: Any cited source does not support its associated claim.")
    assert spec._acceptance_problem(body) == ""
    assert spec._acceptance_problem("**Acceptance criteria**\nObservable result: Make it work.\nFalsifier: TBD")






# ---------------------------------------------------------------------------
# CLI wiring — argparse + _cmd_specify
# ---------------------------------------------------------------------------

def _run_cli(*argv: str) -> int:
    """Invoke the `hermes kanban …` argparse surface directly."""
    root = argparse.ArgumentParser()
    subp = root.add_subparsers(dest="cmd")
    kanban_cli.build_parser(subp)
    ns = root.parse_args(["kanban", *argv])
    return kanban_cli.kanban_command(ns)




@pytest.mark.parametrize("hold", ["needs_input", "decision", "decision_spacing"])
def test_cli_specify_sweep_preserves_decision_records(kanban_home, monkeypatch, capsys, hold):
    with kbc.connect() as conn:
        held = kb.create_task(conn, title="Authoritative title", body="Authoritative premise",
                              assignee="builder", priority=7, tenant="proj-a", triage=hold != "needs_input")
        if hold == "needs_input":
            assert kb.block_task(conn, held, kind="needs_input", reason="Await authorization")
            assert kb.unblock_task(conn, held)
            assert kb.block_task(conn, held, kind="needs_input", reason="Await authorization")
        else:
            marker = "DECISION=B: defer" if hold == "decision" else "decision = A: bounded probe only"
            kb.add_comment(conn, held, "operator", marker)
        before = kb.get_task(conn, held)
        assert before is not None
        assert before.status == "triage"
        comments = [tuple(row) for row in conn.execute("SELECT * FROM task_comments WHERE task_id=?", (held,))]
        events = [tuple(row) for row in conn.execute("SELECT * FROM task_events WHERE task_id=?", (held,))]
        normal = kb.create_task(conn, title="Rough idea", body="Original scope", assignee="builder",
                                priority=3, tenant="proj-a", triage=True)
    calls = []
    body = "**Goal**\nConcrete normal scope." + ACCEPTANCE
    def aux(verb, tid, **kwargs):
        calls.append(tid)
        return jsonlib.dumps({"title": "Refined idea", "body": body}), ""
    monkeypatch.setattr(spec, "_call_aux", aux)
    assert _run_cli("specify", "--all", "--tenant", "proj-a", "--json") == 0
    output = [jsonlib.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    with kbc.connect() as conn:
        after = kb.get_task(conn, held)
        produced = kb.get_task(conn, normal)
        assert after is not None and produced is not None
        print(f"{hold}: held {before.status}->{after.status}; title={after.title!r}; calls={calls}")
        assert after == before
        assert [tuple(row) for row in conn.execute("SELECT * FROM task_comments WHERE task_id=?", (held,))] == comments
        assert [tuple(row) for row in conn.execute("SELECT * FROM task_events WHERE task_id=?", (held,))] == events
        assert produced.status == "ready"
        assert (produced.title, produced.body, produced.assignee, produced.priority, produced.tenant) == (
            "Refined idea", body, "builder", 3, "proj-a")
    assert calls == [normal]
    assert output == [{"task_id": normal, "ok": True, "reason": "specified", "new_title": "Refined idea"}]


@pytest.mark.parametrize("hold", ["needs_input", "decision"])
def test_explicit_specify_refuses_held_triage_card(kanban_home, monkeypatch, hold):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="Keep premise", assignee="builder", triage=hold == "decision")
        if hold == "needs_input":
            assert kb.block_task(conn, tid, kind="needs_input", reason="Await decision")
            assert kb.unblock_task(conn, tid)
            assert kb.block_task(conn, tid, kind="needs_input", reason="Await decision")
        else:
            kb.add_comment(conn, tid, "operator", "DECISION=B: defer")
    def unexpected(*args, **kwargs):
        pytest.fail("Held card must not call auxiliary model")
    monkeypatch.setattr(spec, "_call_aux", unexpected)
    outcome = spec.specify_task(tid)
    assert not outcome.ok
    assert ("needs_input" if hold == "needs_input" else "DECISION") in outcome.reason


def test_cli_specify_tenant_filter(kanban_home, capsys):
    with kbc.connect() as conn:
        outside = kb.create_task(conn, title="outside", triage=True)
        inside = kb.create_task(
            conn, title="inside", triage=True, tenant="proj-a",
        )

    content = jsonlib.dumps({"title": "spec", "body": ACCEPTANCE})
    p, _ = _patch_aux_client(content)
    with p:
        rc = _run_cli("specify", "--all", "--tenant", "proj-a", "--json")
    assert rc == 0
    lines = [
        jsonlib.loads(l)
        for l in capsys.readouterr().out.strip().splitlines()
        if l
    ]
    ids = {row["task_id"] for row in lines}
    assert ids == {inside}

    # The outside task stays in triage.
    with kbc.connect() as conn:
        assert kb.get_task(conn, outside).status == "triage"
        # The inside task was promoted.
        produced = kb.get_task(conn, inside)
        assert produced is not None
        assert produced.status in {"todo", "ready"}
        assert spec._acceptance_problem(produced.body) == ""


