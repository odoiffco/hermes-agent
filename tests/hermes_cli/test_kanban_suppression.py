"""Dispatcher suppression diagnostics expose guarded task identities."""

from hermes_cli.kanban_db_dispatch import DispatchResult, describe_suppression


def test_suppression_names_guarded_task_ids():
    assert describe_suppression([
        DispatchResult(respawn_guarded=[("t_held", "active_pr")])
    ]) == "active_pr=1, guarded_task_ids=t_held"
