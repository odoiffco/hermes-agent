"""Self-continuity distinguishes ledger outcomes from archived prose."""

from cron import executions, jobs
from cron.scheduler_prompt import _inject_context_from


def test_statuses_terminal_only_and_upstream_has_no_status(tmp_path, monkeypatch):
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
    with jobs.use_cron_store(tmp_path):
        directory = jobs.get_cron_output_dir() / "abcdef"
        directory.mkdir(parents=True)
        (directory / "run.md").write_text(
            "# Cron Job: digest\n## Response\n\nledger", encoding="utf-8")
        job = {"id": "abcdef", "context_from": ["self"]}
        assert "Previous run statuses" not in _inject_context_from(job, "next")[0]
        with executions._transaction() as conn:
            for index, (status, error) in enumerate((
                ("failed", "AuthError: bad\ntrace"), ("completed", None),
                ("running", None),
            )):
                conn.execute(
                    "INSERT INTO executions (id, job_id, source, process_id, pid, status, claimed_at, error) "
                    "VALUES (?, 'abcdef', 'builtin', 'test', 1, ?, ?, ?)",
                    (f"{index:08x}", status, f"2026-09-30T0{index}:00:00+08:00", error),
                )
        prompt, injected = _inject_context_from(job, "next")
        assert injected
        assert "completed run=00000001 error=null" in prompt
        assert "failed run=00000000 error=AuthError: bad" in prompt
        assert "trace" not in prompt and "running run=" not in prompt
        upstream, _ = _inject_context_from(
            {"id": "fedcba", "context_from": ["abcdef"]}, "next")
        assert "ledger" in upstream and "Previous run statuses" not in upstream
        monkeypatch.setattr(executions, "list_executions", lambda **kwargs: 1 / 0)
        assert "ledger" in _inject_context_from(job, "next")[0]
        assert "Previous run statuses" not in _inject_context_from(job, "next")[0]
