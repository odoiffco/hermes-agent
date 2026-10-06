"""Both insert paths and the tool preserve runtime intent and visibility."""
import json
from pathlib import Path

import pytest
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect_closing
from hermes_cli.kanban_db_graph import decompose_triage_task


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for key in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with connect_closing(tmp_path / "kanban.db") as conn:
        yield conn


def created(conn, tid):
    return next(e.payload for e in kb.list_events(conn, tid) if e.kind == "created")


@pytest.mark.parametrize("value,expected,source", [(None,3600,"default"), (7200,7200,"explicit"), (0,None,"opt_out")])
def test_creation_precedence(board, value, expected, source):
    tid = kb.create_task(board, title="bound", assignee="default", max_runtime_seconds=value)
    assert kb.get_task(board, tid).max_runtime_seconds == expected
    payload = created(board, tid)
    assert payload["max_runtime_seconds"] == expected
    assert payload["max_runtime_source"] == source


@pytest.mark.parametrize("default", [3600, 0, 4800])
def test_decomposer_and_direct_share_config(board, default):
    from hermes_cli.config import save_config
    save_config({"kanban": {"default_max_runtime_seconds": default}})
    root = kb.create_task(board, title="root", triage=True, assignee="default")
    ids = decompose_triage_task(board, root, root_assignee="default", children=[{"title":"child", "assignee":"default"}])
    for tid in [root, *ids]:
        assert kb.get_task(board, tid).max_runtime_seconds == (default or None)
        assert created(board, tid)["max_runtime_source"] == "default"
        assert created(board, tid)["max_runtime_seconds"] == (default or None)


@pytest.mark.parametrize("value,expected,source", [(None,3600,"default"), (7200,7200,"explicit"), (0,None,"opt_out")])
def test_registry_echo_and_idempotency(board, value, expected, source):
    import tools.kanban_tools
    from tools.registry import registry
    args = {"title":"echo", "assignee":"default", "idempotency_key":"once"}
    if value is not None:
        args["max_runtime_seconds"] = value
    result = json.loads(registry.dispatch("kanban_create", args))
    assert result.get("ok"), result
    assert result["max_runtime_seconds"] == expected
    assert result["max_runtime_source"] == source
    assert kb.get_task(board, result["task_id"]).max_runtime_seconds == expected
    args["max_runtime_seconds"] = 1234
    again = json.loads(registry.dispatch("kanban_create", args))
    assert again["task_id"] == result["task_id"]
    assert again["max_runtime_source"] == source
    assert again["max_runtime_seconds"] == expected


@pytest.mark.parametrize("bound", [0, 3600])
def test_worker_context_visibility(board, bound):
    tid = kb.create_task(board, title="context", assignee="default", max_runtime_seconds=bound)
    lines = []
    kb._ctx_header(lines, kb.get_task(board, tid))
    text = "\n".join(lines)
    assert text.count("Silence policy:") == 1
    if bound:
        assert "Max runtime: 3600s" in text
        assert "UNBOUNDED" not in text
    else:
        assert "Max runtime: UNBOUNDED (explicit opt-out or legacy row) — no wall, no failure accounting" in text


@pytest.mark.parametrize("policy,required,forbidden", [
    ({}, "nudge at 1200s with no durable output, kill at 2400s", "nudge-only"),
    ({"silence_nudge_seconds": 71, "silence_kill_seconds": 0}, "nudge at 71s", "kill at"),
    ({"silence_nudge_seconds": 0, "silence_kill_seconds": 2400}, "Silence policy: disabled", "kill at"),
    ({"silence_nudge_seconds": 0, "silence_kill_seconds": 0}, "Silence policy: disabled", "nudge at"),
    ({"silence_nudge_seconds": 91, "silence_kill_seconds": 181, "silence_kill_limit": 5},
     "after 5 prior silence kills on this task the next kill", "after 3 prior"),
])
def test_context_silence_policy(board, monkeypatch, policy, required, forbidden):
    from hermes_cli import kanban_db_enforcement as enforcement
    monkeypatch.setattr(enforcement, "enforcement_config", lambda: policy)
    tid = kb.create_task(board, title="policy", assignee="default", max_runtime_seconds=3600)
    lines = []
    kb._ctx_header(lines, kb.get_task(board, tid))
    text = "\n".join(lines)
    assert text.count("Silence policy:") == 1
    assert required in text
    assert forbidden not in text
    if policy.get("silence_nudge_seconds", 1200) > 0 and policy.get("silence_kill_seconds", 2400) <= 0:
        assert "nudge-only" in text


def test_context_silence_config_failure_falls_back(board, monkeypatch):
    from hermes_cli import kanban_db_enforcement as enforcement
    tid = kb.create_task(board, title="fallback", assignee="default", max_runtime_seconds=3600)

    def broken():
        raise RuntimeError("unavailable config")

    monkeypatch.setattr(enforcement, "enforcement_config", broken)
    lines = []
    kb._ctx_header(lines, kb.get_task(board, tid))
    text = "\n".join(lines)
    assert text.count("Silence policy:") == 1
    assert "nudge at 1200s" in text
    assert "kill at 2400s" in text
    assert "after 3 prior silence kills on this task the next kill" in text


def test_runtime_config_remains_profile_scoped(board, tmp_path):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from hermes_cli.config import save_config
    from hermes_cli.kanban_db_enforcement import creation_runtime_bound
    for name, bound in [("a", 7200), ("b", 0), ("a", 7200)]:
        home = tmp_path / "profiles" / name
        home.mkdir(parents=True, exist_ok=True)
        token = set_hermes_home_override(home)
        try:
            save_config({"kanban": {"default_max_runtime_seconds":bound}})
            assert creation_runtime_bound() == (bound or None, "default")
        finally:
            reset_hermes_home_override(token)


def test_guidance_requires_bound_and_echo_check():
    from agent.prompt_builder import KANBAN_GUIDANCE
    lifecycle = KANBAN_GUIDANCE.split("7. **If follow-up work appears", 1)[1].split("8. **Flag collision", 1)[0]
    assert "**State a bound.** You MUST pass `max_runtime_seconds`" in lifecycle
    assert 'max_runtime_source: "default"' in lifecycle
    reference = KANBAN_GUIDANCE.split("- **Created cards.**", 1)[1].split("- **Orchestrating:", 1)[0]
    assert "`max_runtime_seconds` / `max_runtime_source`" in reference
