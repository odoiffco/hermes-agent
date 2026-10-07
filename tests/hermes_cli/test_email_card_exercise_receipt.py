"""Receipt runner over the sibling-authored fixtures; no substitute inputs."""
import builtins
import hashlib
import json
import socket
import subprocess
from pathlib import Path
from unittest.mock import Mock

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_email_card as ec
from hermes_cli import kanban_email_board, kanban_email_proposal, kanban_email_record
from tests.hermes_cli.test_kanban_board_content_policy import fresh_home


def test_exercise_receipt(fresh_home, monkeypatch):
    raw = (Path(__file__).resolve().parents[1] / "fixtures" / "email_card_tamper_run.json").read_bytes()
    fixtures = json.loads(raw)
    # Include every supplied input (body/draft/document are separate variants).
    cases = fixtures["tamper_cases"] + [{"name": "positive_control", "body": fixtures["positive_control"], "refusal_code": None}]
    kb.create_board("sis-email")
    runs = []
    deny = Mock(side_effect=AssertionError("network/model/subprocess reached"))
    original_import = builtins.__import__
    def offline_import(name, *args, **kwargs):
        if name.startswith(("openai", "anthropic", "run_agent", "agent", "providers", "litellm", "httpx", "requests", "urllib", "http", "aiohttp")):
            return deny(name)
        return original_import(name, *args, **kwargs)
    with kbc.connect(board="sis-email") as conn:
        before = tuple(conn.iterdump())
        for _ in range(2):
            verdicts = []
            with monkeypatch.context() as scope:
                for owner, attr in ((socket, "socket"), (socket, "create_connection"), (socket, "getaddrinfo"), (subprocess, "Popen")):
                    scope.setattr(owner, attr, deny)
                scope.setattr(builtins, "__import__", offline_import)
                for case in cases:
                    body = case["body"]
                    try:
                        result = ec.validate_email_card_body(conn, body, task_id=fixtures["own_card_id"])
                    except ValueError as exc:
                        code, reason = str(exc).split(": ", 1)
                        assert code == case["refusal_code"] and reason.strip()
                        verdict = {"name": case["name"], "verdict": "refused", "code": code, "reason": reason}
                    else:
                        assert case["refusal_code"] is None and result is None
                        verdict = {"name": case["name"], "verdict": "accepted", "body_unchanged": body.encode() == fixtures["positive_control"].encode()}
                        assert verdict["body_unchanged"]
                    assert body == case["body"]
                    verdicts.append(verdict)
            assert tuple(conn.iterdump()) == before
            runs.append(json.dumps(verdicts, sort_keys=True, ensure_ascii=False).encode())
    deny.assert_not_called()
    assert runs[0] == runs[1]
    primary = [v for v in json.loads(runs[0]) if v["name"] not in {"draft_text", "extracted_document", "positive_control"}]
    assert len(primary) == 8
    assert len({v["reason"] for v in primary}) == 8
    print("EXERCISE_RECEIPT=" + json.dumps({"fixture_sha256": hashlib.sha256(raw).hexdigest(), "run1": json.loads(runs[0]), "run2": json.loads(runs[1]), "byte_identical": True, "verdict_sha256": hashlib.sha256(runs[0]).hexdigest(), "network_model_calls": deny.call_count}, sort_keys=True))
