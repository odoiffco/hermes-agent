"""Dispatcher hydration hold: real board transitions, no worker retry as a probe."""

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_secret_hydration as hydration


@pytest.fixture
def board(tmp_path, monkeypatch):
    # Some neighboring suites evict hermes_cli modules. Resolve a coherent
    # module family at execution time so patches reach production's late imports.
    import importlib
    global kb, kbc, dispatch, hydration
    kb = importlib.import_module("hermes_cli.kanban_db")
    kbc = importlib.import_module("hermes_cli.kanban_db_connect")
    dispatch = importlib.import_module("hermes_cli.kanban_db_dispatch")
    hydration = importlib.import_module("hermes_cli.kanban_secret_hydration")
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    kb.init_db()
    return home


def _die(conn, task_id, pid, output, monkeypatch):
    host = kb._claimer_id().split(":", 1)[0]
    assert kb.claim_task(conn, task_id, claimer=f"{host}:fixture")
    conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (pid, task_id))
    conn.commit()
    monkeypatch.setattr(dispatch, "_worker_final_output", lambda *args, **kwargs: output)
    dispatch._record_worker_exit(pid, 1 << 8)
    return dispatch.detect_crashed_workers(conn)


def test_hydration_failure_is_infrastructure_and_probe_releases_without_a_failed_run(board, monkeypatch):
    monkeypatch.setattr(hydration, "configured_secret_name", lambda *_: "API_KEY")
    probes = []
    monkeypatch.setattr(hydration, "probe", lambda *args: probes.append(args) or False)
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="hydration", assignee="a")
        # A prior work failure must remain unchanged, not reset or incremented.
        conn.execute("UPDATE tasks SET consecutive_failures=1 WHERE id=?", (task_id,))
        conn.commit()
        assert _die(conn, task_id, 72001,
                    "1Password: op read failed for 'op://vault/item/credential': "
                    "could not get item. Too many requests.", monkeypatch) == []
        task = kb.get_task(conn, task_id)
        assert task.status == "blocked"
        assert task.consecutive_failures == 1
        assert task.last_failure_error == "blocked: secret hydration unavailable"
        assert kb.recompute_ready(conn) == 0
        assert not probes
        assert conn.execute("SELECT outcome FROM task_runs WHERE task_id=?", (task_id,)).fetchone()[0] == "secret_hydration_unavailable"
        assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='gave_up'", (task_id,)).fetchone()[0] == 0
        # A dispatcher tick inside the initial delay neither spawns nor probes.
        monkeypatch.setattr(dispatch, "_profile_exists_fn", lambda: lambda _: True)
        calls = []
        result = dispatch.dispatch_once(conn, spawn_fn=lambda *_: calls.append("spawn"))
        assert not result.spawned and not calls
        assert not probes
        event = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='secret_hydration_unavailable' ORDER BY id DESC", (task_id,)).fetchone()
        data = json.loads(event[0])
        monkeypatch.setattr(dispatch.time, "time", lambda: data["next_probe_at"])
        dispatch._resume_secret_hydration(conn)
        assert probes == [("a", "API_KEY")]
        assert kb.get_task(conn, task_id).status == "blocked"
        # The next due probe succeeds. No worker run has occurred in between.
        monkeypatch.setattr(hydration, "probe", lambda *_: True)
        event = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='secret_hydration_unavailable' ORDER BY id DESC", (task_id,)).fetchone()
        monkeypatch.setattr(dispatch.time, "time", lambda: json.loads(event[0])["next_probe_at"])
        dispatch._resume_secret_hydration(conn)
        assert kb.get_task(conn, task_id).status == "ready"
        assert kb.get_task(conn, task_id).consecutive_failures == 1
        assert conn.execute("SELECT count(*) FROM task_runs WHERE task_id=?", (task_id,)).fetchone()[0] == 1
        result = dispatch.dispatch_once(conn, spawn_fn=lambda *_: calls.append("spawn"))
        assert len(result.spawned) == 1 and calls == ["spawn"]


def test_genuine_crash_still_spends_work_budget(board, monkeypatch):
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="crash", assignee="a")
        for pid in (72002, 72003):
            assert _die(conn, task_id, pid, "unrelated worker crash; Too many requests in task prose", monkeypatch) == [task_id]
        task = kb.get_task(conn, task_id)
        assert task.status == "blocked"
        assert task.consecutive_failures == 2
        assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='gave_up'", (task_id,)).fetchone()[0] == 1


def test_evidence_is_not_exit_code_or_ordinary_mention():
    assert hydration.failed_reference("Too many requests, but this is task prose") is None
    assert hydration.failed_reference("1Password docs: op read a secret") is None
    assert hydration.failed_reference("could not read secret 'op://vault/key/credential'") == "op://vault/key/credential"
    assert hydration.failed_reference("could not read secret: upstream unavailable") == ""


def test_reference_free_error_uses_only_unambiguous_mapping(board, monkeypatch):
    monkeypatch.setattr(hydration, "_profile_config", lambda _: (
        board, {"env": {"API_KEY": "op://vault/item/credential"}},
    ))
    assert hydration.configured_secret_name("a", "") == "API_KEY"
    monkeypatch.setattr(hydration, "_profile_config", lambda _: (
        board, {"env": {"FIRST": "op://vault/one/key", "SECOND": "op://vault/two/key"}},
    ))
    assert hydration.configured_secret_name("a", "") is None


def test_persistent_hydration_failure_escalates_without_work_failure(board, monkeypatch):
    monkeypatch.setattr(hydration, "configured_secret_name", lambda *_: "API_KEY")
    monkeypatch.setattr(hydration, "probe", lambda *_: False)
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="permanent outage", assignee="a")
        _die(conn, task_id, 72004, "op read failed for 'op://vault/key/credential'", monkeypatch)
        event = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='secret_hydration_unavailable' ORDER BY id DESC", (task_id,)).fetchone()
        first = json.loads(event[0])["first_seen"]
        monkeypatch.setattr(dispatch.time, "time", lambda: first + hydration.MAX_AGE)
        dispatch._resume_secret_hydration(conn)
        task = kb.get_task(conn, task_id)
        assert task.status == "blocked" and task.consecutive_failures == 0
        assert "operator action required" in task.last_failure_error
        assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='secret_hydration_escalated'", (task_id,)).fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='gave_up'", (task_id,)).fetchone()[0] == 0


def test_probe_uses_profile_bootstrap_not_dispatcher_token(board, monkeypatch):
    from agent.secret_sources import onepassword
    home = board / "profiles" / "a"
    home.mkdir(parents=True)
    (home / ".op.env").write_text("OP_SERVICE_ACCOUNT_TOKEN=profile-token\n")
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "wrong-dispatcher-token")
    monkeypatch.setattr(hydration, "_profile_config", lambda _: (
        home, {"enabled": True, "env": {"API_KEY": "op://vault/item/credential"}},
    ))
    monkeypatch.setattr(onepassword, "find_op", lambda _: Path("/fake/op"))
    seen = []
    def read(_binary, _reference, **kwargs):
        from agent.secret_sources.base import get_source_environment
        seen.append((get_source_environment().get("OP_SERVICE_ACCOUNT_TOKEN"), kwargs["token_value"]))
        return "sensitive value discarded"
    monkeypatch.setattr(onepassword, "_run_op_read", read)
    assert hydration.probe("a", "API_KEY")
    assert seen == [("profile-token", "profile-token")]
