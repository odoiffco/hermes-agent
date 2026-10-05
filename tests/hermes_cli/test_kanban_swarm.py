import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_swarm import (
    SwarmWorkerSpec,
    create_swarm,
    latest_blackboard,
    post_blackboard_update,
)


def worker(*, profile, title, body):
    return SwarmWorkerSpec(
        profile=profile, title=title, body=body,
        acceptance=f"The {title} handoff supplies the requested result with evidence.",
        falsifier=f"The {title} handoff omits the result or supporting evidence.",
    )


def test_swarm_refuses_unspecified_acceptance_without_creating_cards(tmp_path):
    conn = kbc.connect(tmp_path / "kanban.db")
    try:
        for spec, field in [
            (SwarmWorkerSpec("researcher", "Count", "Count records"), "acceptance"),
            (SwarmWorkerSpec("researcher", "Count", "Count records", acceptance="Seven records"), "falsifier"),
            (SwarmWorkerSpec("researcher", "Count", "Count records", acceptance="Seven records", falsifier=" "), "falsifier"),
        ]:
            with pytest.raises(ValueError, match=field):
                create_swarm(conn, goal="Count records", workers=[spec],
                             verifier_assignee="reviewer", synthesizer_assignee="writer")
            assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    finally:
        conn.close()


def test_swarm_cli_refuses_missing_or_blank_criteria(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from hermes_cli import kanban as cli

    @contextmanager
    def scratch_board():
        conn = kbc.connect(tmp_path / "cli.db")
        try:
            yield conn
        finally:
            conn.close()

    monkeypatch.setattr(cli.kbc, "connect_closing", scratch_board)
    base = 'swarm "Count citations" --worker "researcher:Citations" --verifier reviewer --synthesizer writer'
    assert "one --acceptance" in cli.run_slash(base)
    assert "falsifier is required" in cli.run_slash(base + ' --acceptance "three citations" --falsifier " "')
    with kbc.connect(tmp_path / "cli.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    output = cli.run_slash(base + ' --acceptance "three citations" --falsifier "fewer than three"')
    assert "error:" not in output
    with kbc.connect(tmp_path / "cli.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 4


def test_non_code_worker_and_synthesizer_have_falsifiable_outcomes(tmp_path):
    conn = kbc.connect(tmp_path / "kanban.db")
    try:
        created = create_swarm(
            conn, goal="Produce a cited evidence memo",
            workers=[SwarmWorkerSpec(
                "researcher", "Cited evidence", "Collect cited sources",
                acceptance="Three independently cited primary sources in the handoff.",
                falsifier="Fewer than three distinct primary citations are present.",
            )], verifier_assignee="reviewer", synthesizer_assignee="writer",
        )
        cards = [kb.get_task(conn, tid) for tid in (
            created.root_id, *created.worker_ids, created.verifier_id, created.synthesizer_id,
        )]
        bodies = [card.body if card is not None else "" for card in cards]
        assert all("Observable result:" in (body or "") and "Falsifier:" in (body or "")
                   for body in bodies)
        assert "Fewer than three distinct primary citations" in (bodies[1] or "")
        assert "traces each substantive conclusion" in (bodies[-1] or "")
        assert "cannot be traced" in (bodies[-1] or "")
    finally:
        conn.close()


def test_create_swarm_builds_parallel_workers_verifier_and_synthesizer(tmp_path):
    conn = kbc.connect(tmp_path / "kanban.db")
    try:
        created = create_swarm(
            conn,
            goal="Map the target market and produce a decision memo.",
            workers=[
                worker(profile="researcher-a", title="Market scan", body="Find competitors"),
                worker(profile="researcher-b", title="Customer scan", body="Find customer pains"),
            ],
            verifier_assignee="reviewer",
            synthesizer_assignee="writer",
            tenant="intel",
            created_by="orchestrator",
        )

        root = kb.get_task(conn, created.root_id)
        workers = [kb.get_task(conn, tid) for tid in created.worker_ids]
        verifier = kb.get_task(conn, created.verifier_id)
        synthesizer = kb.get_task(conn, created.synthesizer_id)

        assert root is not None
        assert all(task is not None for task in workers)
        workers = [task for task in workers if task is not None]
        assert verifier is not None
        assert synthesizer is not None
        assert root.status == "done"
        assert root.assignee == "orchestrator"
        assert [task.status for task in workers] == ["ready", "ready"]
        assert [task.assignee for task in workers] == ["researcher-a", "researcher-b"]
        assert verifier.status == "todo"
        assert synthesizer.status == "todo"
        assert set(kb.parent_ids(conn, created.verifier_id)) == set(created.worker_ids)
        assert kb.parent_ids(conn, created.synthesizer_id) == [created.verifier_id]
        assert all(created.root_id in (task.body or "") for task in workers)
    finally:
        conn.close()


def test_create_swarm_graph_is_atomic_and_rolls_back_partial_build(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    db_path = tmp_path / "kanban.db"
    writer = kbc.connect(db_path)
    reader = kbc.connect(db_path)
    original_create = kb.create_task
    original_complete = kb.complete_task
    calls = 0

    def observed_create(*args, **kwargs):
        nonlocal calls
        calls += 1
        task_id = original_create(*args, **kwargs)
        if calls == 1:
            # Releasing the nested create_task savepoint must not expose the
            # root before the whole graph's outer transaction commits.
            visible = reader.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            assert visible == 0
        if calls == 3:
            raise RuntimeError("synthetic graph-construction failure")
        return task_id

    monkeypatch.setattr(kb, "create_task", observed_create)
    try:
        with pytest.raises(RuntimeError, match="synthetic graph-construction failure"):
            create_swarm(
                writer,
                goal="Build atomically",
                workers=[
                    worker(profile="worker-a", title="A", body="A"),
                    worker(profile="worker-b", title="B", body="B"),
                ],
                verifier_assignee="reviewer",
                synthesizer_assignee="writer",
            )
        assert writer.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        assert reader.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0

        monkeypatch.setattr(kb, "create_task", original_create)
        import hermes_cli.kanban_swarm as ks

        original_activate = ks._activate_root_inline
        monkeypatch.setattr(
            ks, "_activate_root_inline", lambda *args, **kwargs: False
        )
        with pytest.raises(RuntimeError, match="could not activate"):
            create_swarm(
                writer,
                goal="Fail activation atomically",
                workers=[
                    worker(profile="worker-a", title="A", body="A"),
                ],
                verifier_assignee="reviewer",
                synthesizer_assignee="writer",
            )
        assert writer.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        assert reader.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0

        hooks: list[tuple[str, bool]] = []
        monkeypatch.setattr(ks, "_activate_root_inline", original_activate)
        monkeypatch.setattr(
            kb,
            "_fire_kanban_lifecycle_hook",
            lambda event, *_args, **_kwargs: hooks.append(
                (event, writer.in_transaction)
            ),
        )
        create_swarm(
            writer,
            goal="Commit before lifecycle hook",
            workers=[worker(profile="worker-a", title="A", body="A")],
            verifier_assignee="reviewer",
            synthesizer_assignee="writer",
        )
        assert hooks == [("kanban_task_completed", False)]
    finally:
        reader.close()
        writer.close()


def test_plain_write_txn_nesting_raises_and_allow_nested_composes(tmp_path):
    """B1 regression: nesting is explicit opt-in, never silent.

    Plain ``write_txn`` inside an open transaction must raise loudly (the
    historical invariant). ``allow_nested=True`` composes via a savepoint,
    and an outer rollback discards the inner work without any post-commit
    side effects having fired (the workspace directory survives).
    """
    conn = kbc.connect(tmp_path / "kanban.db")
    try:
        workspace = tmp_path / "scratch-ws"
        workspace.mkdir()
        tid = kb.create_task(conn, title="ws task", assignee="worker")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET workspace_path = ? WHERE id = ?",
                (str(workspace), tid),
            )

        # 1) Plain nesting raises loudly.
        with pytest.raises(RuntimeError, match="already inside a transaction"):
            with kb.write_txn(conn):
                with kb.write_txn(conn):
                    pass
        assert not conn.in_transaction

        # 2) allow_nested composes; outer rollback discards inner work
        #    and no side effects (workspace cleanup) fired meanwhile.
        with pytest.raises(RuntimeError, match="outer failure"):
            with kb.write_txn(conn):
                with kb.write_txn(conn, allow_nested=True):
                    conn.execute(
                        "UPDATE tasks SET status = 'done' WHERE id = ?", (tid,)
                    )
                    kb._append_event(conn, tid, "completed", {"result_len": 0})
                # Inner savepoint released, but the outer txn now fails.
                raise RuntimeError("outer failure")
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "ready"  # inner 'done' flip was discarded
        assert not any(
            e.kind == "completed" for e in kb.list_events(conn, tid)
        )
        assert workspace.is_dir()  # no _cleanup_workspace side effect fired
    finally:
        conn.close()


def test_swarm_blackboard_merges_structured_updates(tmp_path):
    conn = kbc.connect(tmp_path / "kanban.db")
    try:
        created = create_swarm(
            conn,
            goal="Collect evidence.",
            workers=[worker(profile="researcher", title="Evidence", body="Find proof")],
            verifier_assignee="reviewer",
            synthesizer_assignee="writer",
        )

        post_blackboard_update(
            conn,
            created.root_id,
            author="researcher",
            key="sources",
            value=["https://example.com/a"],
        )
        post_blackboard_update(
            conn,
            created.root_id,
            author="reviewer",
            key="risks",
            value={"missing_primary_source": True},
        )

        board = latest_blackboard(conn, created.root_id)
        assert board["sources"] == ["https://example.com/a"]
        assert board["risks"] == {"missing_primary_source": True}
        assert board["_authors"]["sources"] == "researcher"
    finally:
        conn.close()


def test_swarm_verifier_and_synthesis_are_dependency_gated(tmp_path):
    conn = kbc.connect(tmp_path / "kanban.db")
    try:
        created = create_swarm(
            conn,
            goal="Research two branches then verify and synthesize.",
            workers=[
                worker(profile="a", title="Branch A", body="A"),
                worker(profile="b", title="Branch B", body="B"),
            ],
            verifier_assignee="reviewer",
            synthesizer_assignee="writer",
        )

        kb.complete_task(
            conn,
            created.worker_ids[0],
            summary="A done",
            metadata={"confidence": 0.8},
        )
        kb.recompute_ready(conn)
        verifier = kb.get_task(conn, created.verifier_id)
        synthesizer = kb.get_task(conn, created.synthesizer_id)
        assert verifier is not None
        assert synthesizer is not None
        assert verifier.status == "todo"
        assert synthesizer.status == "todo"

        kb.complete_task(conn, created.worker_ids[1], summary="B done")
        kb.recompute_ready(conn)
        verifier = kb.get_task(conn, created.verifier_id)
        synthesizer = kb.get_task(conn, created.synthesizer_id)
        assert verifier is not None
        assert synthesizer is not None
        assert verifier.status == "ready"
        assert synthesizer.status == "todo"

        kb.complete_task(
            conn,
            created.verifier_id,
            summary="Verified both branches",
            metadata={"gate": "pass"},
        )
        kb.recompute_ready(conn)
        synthesizer = kb.get_task(conn, created.synthesizer_id)
        assert synthesizer is not None
        assert synthesizer.status == "ready"
    finally:
        conn.close()
