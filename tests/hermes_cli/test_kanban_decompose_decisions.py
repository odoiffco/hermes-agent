"""Real-store regression for decision escalations and executing child routing."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_decompose as decomp
from hermes_cli.kanban_db_graph import decompose_triage_task

ACCEPTANCE = "**Acceptance criteria**\nObservable result: Local test passes.\nFalsifier: Local test fails."


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    names = ["router", "builder", "reviewer"]
    monkeypatch.setattr(decomp.profiles_mod, "list_profiles", lambda: [
        SimpleNamespace(name=n, description=n) for n in names])
    monkeypatch.setattr(decomp.profiles_mod, "profile_exists", lambda n: n in names)
    monkeypatch.setattr(decomp.profiles_mod, "get_active_profile_name", lambda: "router")
    # Real config loader, not a mock of its effective result.
    (home / "config.yaml").write_text("kanban:\n  orchestrator_profile: router\n  default_assignee: router\n")
    return home


def graph(count=5):
    return {"fanout": True, "tasks": [
        {"title": f"Implement slice {i}", "body": f"Implement slice {i}.\n{ACCEPTANCE}",
         "assignee": "router" if i == 0 else None,
         "parents": [] if i == 0 else [i - 1]} for i in range(count)]}


def escalate(conn, tid):
    assert kb.block_task(conn, tid, kind="needs_input", reason="Missing authorization")
    assert kb.unblock_task(conn, tid)
    assert kb.block_task(conn, tid, kind="needs_input", reason="Still missing authorization")
    root = kb.get_task(conn, tid)
    assert root.status == "triage" and root.block_kind == "needs_input"
    assert root.block_recurrences == 2


@pytest.mark.parametrize("escalated", [False, True])
def test_actual_needs_input_block_never_creates_children(board, monkeypatch, escalated):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="Update runtime", assignee="builder")
        if escalated:
            escalate(conn, tid)
        else:
            assert kb.block_task(conn, tid, kind="needs_input", reason="Await decision")
    def unexpected(*args, **kwargs):
        pytest.fail("Decision-blocked card must not call auxiliary model")
    monkeypatch.setattr(decomp, "_call_aux", unexpected)
    assert tid not in decomp.list_triage_ids()
    outcome = decomp.decompose_task(tid)
    assert not outcome.ok
    with kbc.connect() as conn:
        assert decompose_triage_task(conn, tid, root_assignee="router", children=graph()["tasks"]) is None
        assert len(kb.list_tasks(conn)) == 1
        assert not conn.execute("SELECT 1 FROM task_links").fetchall()
    print(f"needs_input escalated={escalated}: children=[] assignee=[] priority=[] premise=[]")


@pytest.mark.parametrize("decision", ["B: Defer update and gateway cycle — DEFERRED INDEFINITELY", "A: approve only the bounded probe"])
def test_decision_comment_cannot_be_replaced_with_false_approval(board, monkeypatch, decision):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="Update runtime", assignee="builder", triage=True)
        kb.add_comment(conn, tid, "desktop", f"DECISION={decision}")
    malicious = graph(4)
    malicious["tasks"][0]["body"] = "Approval has been recorded and the supported mechanism established.\n" + ACCEPTANCE
    calls = []
    monkeypatch.setattr(decomp, "_call_aux", lambda *a, **kw: (calls.append(kw) or json.dumps(malicious), ""))
    assert tid not in decomp.list_triage_ids()
    outcome = decomp.decompose_task(tid)
    assert not outcome.ok and "DECISION" in outcome.reason
    assert calls == []
    with kbc.connect() as conn:
        assert decompose_triage_task(conn, tid, root_assignee="router", children=malicious["tasks"]) is None
        cards = kb.list_tasks(conn)
        assert len(cards) == 1 and cards[0].id == tid
        # Read every persisted premise; no generated approval or mechanism exists.
        assert all("Approval has been recorded" not in (c.body or "") for c in cards)
        assert conn.execute("SELECT body FROM task_comments WHERE task_id=?", (tid,)).fetchone()[0] == f"DECISION={decision}"
    print(f"DECISION={decision}: children=[] premise=[] (false approval payload refused)")


def test_decision_arriving_during_aux_call_prevents_atomic_fanout(board, monkeypatch):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="Implement", assignee="builder", triage=True)
    def aux(*args, **kwargs):
        with kbc.connect() as conn:
            kb.add_comment(conn, tid, "desktop", "DECISION=B: defer indefinitely")
        return json.dumps(graph()), ""
    monkeypatch.setattr(decomp, "_call_aux", aux)
    assert not decomp.decompose_task(tid).ok
    with kbc.connect() as conn:
        assert len(kb.list_tasks(conn)) == 1
        assert not conn.execute("SELECT 1 FROM task_links").fetchall()


def test_normal_five_slice_graph_retains_shape_rollup_and_execution_owner(board, monkeypatch):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="Implement validator", assignee="builder", triage=True, priority=7)
    payload = graph()
    monkeypatch.setattr(decomp, "_call_aux", lambda *a, **kw: (json.dumps(payload), ""))
    assert tid in decomp.list_triage_ids()
    result = decomp.decompose_task(tid)
    assert result.ok and len(result.child_ids) == len(payload["tasks"])
    with kbc.connect() as conn:
        root = kb.get_task(conn, tid)
        children = [kb.get_task(conn, cid) for cid in result.child_ids]
        assert root.assignee == "router" and root.status == "todo"
        assert all(c.assignee == "builder" for c in children)
        assert [c.status for c in children] == ["ready"] + ["todo"] * 4
        # Children inherit the parent's p7 under the sibling priority fix.
        assert all(c.priority == 7 for c in children)
        assert [c.body for c in children] == [x["body"] for x in payload["tasks"]]
        links = {tuple(row) for row in conn.execute("SELECT parent_id,child_id FROM task_links")}
        expected = {(cid, tid) for cid in result.child_ids}
        expected.update(zip(result.child_ids, result.child_ids[1:]))
        assert links == expected
    print("normal children:", [(c.assignee, c.priority, c.status, c.body) for c in children])


def test_explicit_executing_default_stays_authoritative(board, monkeypatch):
    (board / "config.yaml").write_text("kanban:\n  orchestrator_profile: router\n  default_assignee: reviewer\n")
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="Implement", assignee="builder", triage=True)
    payload = graph(2)
    for child in payload["tasks"]:
        child["assignee"] = "unknown"
    monkeypatch.setattr(decomp, "_call_aux", lambda *a, **kw: (json.dumps(payload), ""))
    result = decomp.decompose_task(tid)
    assert result.ok
    with kbc.connect() as conn:
        children = [kb.get_task(conn, cid) for cid in result.child_ids]
        assert all(c.assignee == "reviewer" for c in children)
    print("explicit default children:", [(c.assignee, c.priority, c.body) for c in children])


def test_router_parent_without_execution_owner_refuses_graph(board, monkeypatch):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="Implement", assignee="router", triage=True)
    monkeypatch.setattr(decomp, "_call_aux", lambda *a, **kw: (json.dumps(graph()), ""))
    result = decomp.decompose_task(tid)
    assert not result.ok and "executing parent" in result.reason
    with kbc.connect() as conn:
        assert len(kb.list_tasks(conn)) == 1


def test_tick_cap_skips_decision_holds_without_spending_attempts(board, monkeypatch):
    from gateway import kanban_watchers_dispatcher as kwd

    with kbc.connect() as conn:
        held = kb.create_task(conn, title="Await authorization", assignee="builder")
        escalate(conn, held)
        commented = kb.create_task(conn, title="Deferred", assignee="builder", triage=True)
        kb.add_comment(conn, commented, "desktop", "DECISION=B: defer indefinitely")
        normal = [kb.create_task(conn, title=f"Implement {i}", assignee="builder", triage=True) for i in range(3)]
    calls = []
    def aux(verb, tid, **kwargs):
        calls.append(tid)
        return json.dumps(graph(2)), ""
    monkeypatch.setattr(decomp, "_call_aux", aux)
    settings = kwd._DispatcherSettings(60.0, None, None, 2, 0, True, None, None)
    dispatcher = kwd._KanbanDispatcher(SimpleNamespace(DEFAULT_BOARD="default"), settings)
    monkeypatch.setattr(dispatcher, "_board_slugs", lambda: ["default"])
    assert dispatcher.auto_decompose_tick(2) == 2
    assert len(calls) == 2 and set(calls).issubset(normal)
    remaining = decomp.list_triage_ids()
    assert len(remaining) == 1 and remaining[0] in normal
    with kbc.connect() as conn:
        assert kb.get_task(conn, held).status == "triage"
        assert kb.get_task(conn, commented).status == "triage"
    print("tick cap=2: two normal graphs created; needs_input/DECISION held; one normal deferred")


def test_needs_input_escalation_during_aux_is_refused_by_store(board, monkeypatch):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="Implement", assignee="builder", triage=True)
    def aux(*args, **kwargs):
        with kbc.connect() as conn:
            assert kb.specify_triage_task(conn, tid, body=ACCEPTANCE)
            kb.recompute_ready(conn)
            escalate(conn, tid)
        return json.dumps(graph()), ""
    monkeypatch.setattr(decomp, "_call_aux", aux)
    assert not decomp.decompose_task(tid).ok
    with kbc.connect() as conn:
        assert len(kb.list_tasks(conn)) == 1
        assert not conn.execute("SELECT 1 FROM task_links").fetchall()


def test_transient_escalation_still_decomposes_normally(board, monkeypatch):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="Implement", assignee="builder")
        assert kb.block_task(conn, tid, kind="transient", reason="Retryable failure")
        assert kb.unblock_task(conn, tid)
        assert kb.block_task(conn, tid, kind="transient", reason="Retryable failure")
        assert kb.get_task(conn, tid).status == "triage"
    monkeypatch.setattr(decomp, "_call_aux", lambda *a, **kw: (json.dumps(graph()), ""))
    assert tid in decomp.list_triage_ids()
    result = decomp.decompose_task(tid)
    assert result.ok and len(result.child_ids) == 5
    with kbc.connect() as conn:
        children = [kb.get_task(conn, cid) for cid in result.child_ids]
        assert all(c.assignee == "builder" for c in children)
    print("transient children:", [(c.assignee, c.priority, c.body) for c in children])
