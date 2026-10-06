"""LLM-free supersession contracts: real decomposition and SQLite graph."""
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_db_graph import decompose_triage_task


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def family(conn):
    root = kb.create_task(conn, title="R", triage=True)
    children = decompose_triage_task(
        conn, root, root_assignee="mgr", author="decomposer",
        children=[
            {"title": "A", "assignee": "worker", "parents": []},
            {"title": "B", "assignee": "worker", "parents": [0]},
            {"title": "C", "assignee": "worker", "parents": [1]},
        ],
    )
    return (*children, root)


def supersede(conn, anchor):
    return kb.supersede_family(conn, anchor, reason="ruling X", author="mgr")


def stamps(conn, tid):
    return [c for c in kb.list_comments(conn, tid) if c.body.startswith("SUPERSEDED")]


def test_fixture_sanity(kanban_home):
    with kbc.connect() as conn:
        ids = family(conn)
        assert [kb.get_task(conn, tid).status for tid in ids] == ["ready", "todo", "todo", "todo"]


def test_containment_and_authoritative_stamps(kanban_home):
    with kbc.connect() as conn:
        ids = family(conn)
        result = supersede(conn, ids[0])
        assert result["ok"] and set(result["archived"]) == set(ids)
        for tid in ids:
            assert kb.get_task(conn, tid).status == "archived"
            assert len(stamps(conn, tid)) == 1
            stamp = stamps(conn, tid)[0]
            assert stamp.author == "mgr"
            assert "ruling X" in stamp.body and ids[0] in stamp.body
            assert "authoritative over any contradicting body line" in stamp.body
            assert stamp.body in kb.build_worker_context(conn, tid)
            assert any(e.kind == "commented" for e in kb.list_events(conn, tid))


def test_archive_events_reverse_topological(kanban_home):
    with kbc.connect() as conn:
        a, b, c, r = family(conn)
        result = supersede(conn, a)
        order = [row[0] for row in conn.execute("SELECT task_id FROM task_events WHERE kind='archived' ORDER BY id")]
        assert order == result["archived"] == result["stamped"] == [r, c, b, a]


def test_no_family_promotions_after_sweep(kanban_home, monkeypatch):
    with kbc.connect() as conn:
        ids = family(conn)
        original = kb.recompute_ready
        calls = []

        def recompute(c):
            assert not c.in_transaction
            assert all(kb.get_task(c, tid).status == "archived" for tid in ids)
            calls.append(True)
            return original(c)

        monkeypatch.setattr(kb, "recompute_ready", recompute)
        supersede(conn, ids[0])
        assert len(calls) == 1
        assert original(conn) == 0
        assert all(kb.get_task(conn, tid).status == "archived" for tid in ids)


def test_done_card_honesty(kanban_home):
    with kbc.connect() as conn:
        a, b, c, r = family(conn)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (b,))
        result = supersede(conn, a)
        assert kb.get_task(conn, b).status == "done"
        assert result["left_terminal"] == [{"id": b, "status": "done"}]
        assert set(result["archived"]) == {a, c, r}
        assert len(stamps(conn, b)) == 1


def test_idempotence(kanban_home):
    with kbc.connect() as conn:
        ids = family(conn)
        supersede(conn, ids[0])
        before = conn.execute("SELECT count(*) FROM task_events").fetchone()[0]
        assert supersede(conn, ids[0])["no_op"] is True
        assert conn.execute("SELECT count(*) FROM task_events").fetchone()[0] == before
        assert all(len(stamps(conn, tid)) == 1 for tid in ids)


def test_umbrella_scope_is_descendants_not_ancestors(kanban_home):
    with kbc.connect() as conn:
        a, b, c, r = family(conn)
        downstream = kb.create_task(conn, title="after R", assignee="worker", parents=[r])
        result = supersede(conn, r)
        assert set(result["archived"]) == set(result["stamped"]) == {r, downstream}
        assert [kb.get_task(conn, tid).status for tid in (a, b, c)] == ["ready", "todo", "todo"]
        assert all(not stamps(conn, tid) for tid in (a, b, c))
