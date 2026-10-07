"""CLI-owned board subject policy, with fail-closed reads.

Board import deliberately does not carry this policy through: imported boards
revert to fail-closed until the operator explicitly allows subjects via the CLI.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from hermes_cli import kanban_db as kb
from hermes_cli.kanban import _DELEGATED_CHILD_DENIED_BOARD_ACTIONS, _is_delegated_child_cli_mutation
from hermes_cli.kanban_boards import _BOARD_HANDLERS


@pytest.fixture
def fresh_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_HOME", "HERMES_KANBAN_BOARD"):
        monkeypatch.delenv(var, raising=False)
    import hermes_constants
    hermes_constants._cached_default_hermes_root = None
    kb._INITIALIZED_PATHS.clear()
    return home


def test_absent_policy_fails_closed(fresh_home):
    kb.create_board("testb")
    assert kb.board_subject_permitted("testb") is False
    assert "content" not in kb.read_board_metadata("testb")


def test_allow_persists_and_reads_true(fresh_home):
    kb.write_board_metadata("testb", subject_permitted=True)
    raw = json.loads(kb.board_metadata_path("testb").read_text())
    assert raw["content"] == {"subject_permitted": True}
    assert kb.board_subject_permitted("testb") is True


def test_deny_is_explicit_not_erasure(fresh_home):
    kb.write_board_metadata("testb", subject_permitted=True)
    kb.write_board_metadata("testb", subject_permitted=False)
    raw = json.loads(kb.board_metadata_path("testb").read_text())
    assert raw["content"]["subject_permitted"] is False
    assert kb.board_subject_permitted("testb") is False


def test_unmentioned_policy_is_preserved(fresh_home):
    kb.write_board_metadata("testb", subject_permitted=True)
    kb.write_board_metadata("testb", name="X")
    raw = json.loads(kb.board_metadata_path("testb").read_text())
    assert raw["name"] == "X"
    assert raw["content"]["subject_permitted"] is True
    assert kb.board_subject_permitted("testb") is True


@pytest.mark.parametrize("content", [
    {"subject_permitted": "yes"}, "nope", {"subject_permitted": None},
    {"subject_permitted": 1}, {},
])
def test_malformed_policy_fails_closed(fresh_home, content):
    path = kb.board_metadata_path("testb")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"content": content}))
    assert kb.board_subject_permitted("testb") is False


def test_cli_handler_allow_deny_roundtrip(fresh_home, capsys):
    kb.create_board("testb")
    handler = _BOARD_HANDLERS["set-subject-policy"]
    for mode, permitted in (("allow", True), ("deny", False)):
        assert handler(argparse.Namespace(slug="testb", mode=mode)) == 0
        assert kb.board_subject_permitted("testb") is permitted
        assert json.loads(kb.board_metadata_path("testb").read_text())["content"]["subject_permitted"] is permitted
        assert f"subject policy set to {mode}" in capsys.readouterr().out


def test_cli_handler_requires_existing_board(fresh_home):
    assert _BOARD_HANDLERS["set-subject-policy"](argparse.Namespace(slug="missing", mode="allow")) != 0
    assert not kb.board_metadata_path("missing").exists()


def test_delegated_child_guard(fresh_home, monkeypatch):
    assert "set-subject-policy" in _DELEGATED_CHILD_DENIED_BOARD_ACTIONS
    kb.write_board_metadata("testb", subject_permitted=False)
    path = kb.board_metadata_path("testb")
    before = path.read_bytes()
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", str(fresh_home))
    args = argparse.Namespace(kanban_action="boards", boards_action="set-subject-policy", slug="testb", mode="allow")
    assert _is_delegated_child_cli_mutation(args) is True
    with pytest.raises(PermissionError):
        kb.write_board_metadata("testb", subject_permitted=True)
    assert path.read_bytes() == before
