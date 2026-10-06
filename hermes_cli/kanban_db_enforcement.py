"""Shared durable-output and bounded enforcement policy (no worker-loop liveness)."""
import json


def enforcement_config() -> dict:
    from hermes_cli.config import load_config_readonly
    return (load_config_readonly() or {}).get("kanban", {}) or {}


def creation_runtime_bound(value=None) -> tuple:
    """Resolve the shared insertion policy and retain the creator's intent."""
    from hermes_cli.kanban_db import _opt_int

    source = "default" if value is None else "explicit"
    if value is None:
        value = enforcement_config().get("default_max_runtime_seconds", 3600)
    value = _opt_int(value)
    if value is not None and value <= 0:
        value = None
        if source == "explicit":
            source = "opt_out"
    return value, source


def durable_output_count(conn, task_id, profile, started_at, now) -> int:
    return conn.execute(
        "SELECT (SELECT COUNT(*) FROM task_comments WHERE task_id = ? AND author = ? "
        "AND created_at BETWEEN ? AND ?) + "
        "(SELECT COUNT(*) FROM task_attachments WHERE task_id = ? AND uploaded_by = ? "
        "AND created_at BETWEEN ? AND ?)",
        (task_id, profile, started_at, now, task_id, profile, started_at, now),
    ).fetchone()[0]


def enforcement_count(conn, task_id, outcome, *, productive=False) -> int:
    rows = conn.execute("SELECT metadata FROM task_runs WHERE task_id = ? AND outcome = ?",
                        (task_id, outcome)).fetchall()
    if not productive:
        return len(rows)
    return sum(bool(json.loads(row["metadata"] or "{}").get("productive_wall")) for row in rows)
