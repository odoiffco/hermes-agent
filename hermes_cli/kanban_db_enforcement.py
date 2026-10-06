"""Shared durable-output and bounded enforcement policy (no worker-loop liveness)."""
import json


def enforcement_config() -> dict:
    from hermes_cli.config import load_config_readonly
    return (load_config_readonly() or {}).get("kanban", {}) or {}


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
