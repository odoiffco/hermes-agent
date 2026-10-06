"""Observation gate fixtures exercise the real read-only DB path and CLI."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "measure_first_contribution.py"
spec = importlib.util.spec_from_file_location("measure_first_contribution", SCRIPT)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)
D, E = 10000, 20000


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "board.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, profile TEXT,
          started_at INTEGER, ended_at INTEGER, outcome TEXT, summary TEXT);
        CREATE TABLE task_comments (id INTEGER PRIMARY KEY, task_id TEXT, author TEXT,
          body TEXT, created_at INTEGER);
        CREATE TABLE task_attachments (id INTEGER PRIMARY KEY, task_id TEXT, uploaded_by TEXT,
          created_at INTEGER, size INTEGER);
    """)
    yield path, conn
    conn.close()


def run(conn, start, outcome="completed", summary=None, duration=1000):
    rid = conn.execute("SELECT coalesce(max(id),0)+1 FROM task_runs").fetchone()[0]
    conn.execute("INSERT INTO task_runs VALUES (?,?,?,?,?,?,?)",
                 (rid, f"t_{rid}", "worker", start, start + duration, outcome, summary))
    return rid


def comment(conn, rid, when, text="Progress: " + "x" * 120, author="worker"):
    conn.execute("INSERT INTO task_comments(task_id,author,body,created_at) VALUES (?,?,?,?)",
                 (f"t_{rid}", author, text, when))


def populate(conn, pre_ttc=700, post_ttc=200, n=100):
    for epoch, ttc in ((1000, pre_ttc), (E, post_ttc)):
        for i in range(n):
            start = epoch + i
            rid = run(conn, start, "timed_out")
            comment(conn, rid, start + ttc)
    conn.commit()


def report(db, **kwargs):
    path, conn = db
    conn.commit()
    return gate.measure(path, D, E, **kwargs)


def test_silence_killed_and_unknown_outcomes(db):
    path, conn = db
    run(conn, 1000, "silence_killed")
    run(conn, E, "silence_killed")
    run(conn, E, "new_terminal_outcome")
    run(conn, E, "crashed")
    run(conn, E, "")
    run(conn, E, None)
    result = report(db)
    assert result["pre"]["n"] == result["post"]["n"] == 1
    assert result["post"]["silence_killed_per_100"] == 100
    assert result["post"]["excluded_counts"] == {"new_terminal_outcome": 1, "crashed": 1, "<NULL/EMPTY>": 2}
    assert result["unknown_outcomes"] == {"new_terminal_outcome": 1}
    assert "UNKNOWN-OUTCOME" in gate.render(result)


@pytest.mark.parametrize("outcome", ["completed", "review_requested", "blocked"])
def test_intent_only_is_not_contributed(db, outcome):
    path, conn = db
    rid = run(conn, E, outcome)
    comment(conn, rid, E + 600)
    comment(conn, rid, E + 1)
    result = report(db)
    assert result["post"]["not_contributed_share"] == 1
    assert result["post"]["late_or_never_share"] == 1
    assert result["post"]["median_ttc_seconds"] is None
    assert result["post"]["runs"][0]["intent_only"]
    assert len(result["intent_only_comments"]) == 2
    assert result["intent_only_comments"][0]["body"] == "Progress: " + "x" * 120


def test_post_600_comment_is_contribution(db):
    path, conn = db
    rid = run(conn, E)
    comment(conn, rid, E + 601)
    result = report(db)
    assert result["post"]["not_contributed_share"] == 0
    assert result["post"]["median_ttc_seconds"] == 601
    assert result["post"]["late_or_never_share"] == 1


@pytest.mark.parametrize("kind", ["attachment", "summary", "late_progress", "other_outcome"])
def test_intent_guard_exceptions(db, kind):
    path, conn = db
    rid = run(conn, E, "gave_up" if kind == "other_outcome" else "completed",
              "s" * 120 if kind == "summary" else None)
    comment(conn, rid, E + 10)
    if kind == "attachment":
        conn.execute("INSERT INTO task_attachments(task_id,uploaded_by,created_at,size) VALUES (?,?,?,0)",
                     (f"t_{rid}", "worker", E + 20))
    if kind == "late_progress":
        comment(conn, rid, E + 601)
    result = report(db)
    assert result["post"]["not_contributed_share"] == 0
    assert result["post"]["median_ttc_seconds"] == 10


def test_attribution_window_and_length(db):
    path, conn = db
    rid = run(conn, E, summary="s" * 120)
    comment(conn, rid, E + 1, author="someone_else")
    comment(conn, rid, E - 1)
    comment(conn, rid, E + 1001)
    comment(conn, rid, E + 2, text="s" * 119)
    result = report(db)
    assert result["post"]["median_ttc_seconds"] == 1000


def test_equal_medians_fail_and_strict_fall_passes(db):
    path, conn = db
    populate(conn, pre_ttc=200, post_ttc=200)
    result = report(db)
    assert result["verdict"] == "FAIL"
    assert "RED: median TTC did not strictly fall" in gate.render(result)
    conn.execute("UPDATE task_comments SET created_at=created_at-1 WHERE created_at>=?", (E,))
    result = report(db)
    assert result["verdict"] == "PASS"
    assert result["exit_code"] == 0


@pytest.mark.parametrize("n,deploy,minimum", [(99,E,100), (100,None,100), (99,E,1)])
def test_insufficient_data_exit_two(db, n, deploy, minimum):
    path, conn = db
    populate(conn, n=n)
    cmd = [sys.executable, str(SCRIPT), "--db", str(path), "--landing-epoch", str(D), "--min-n", str(minimum)]
    if deploy is not None:
        cmd += ["--deploy-epoch", str(deploy)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode == 2
    assert result.stdout.count("VERDICT:") == 1
    assert "VERDICT: INSUFFICIENT DATA" in result.stdout
    assert "RED:" not in result.stdout


def test_fixed_cohorts_and_overlap_exclusion(db):
    path, conn = db
    populate(conn, n=151)
    run(conn, E - 500, duration=1000)  # crosses deployment, belongs to neither
    result = report(db)
    assert result["pre"]["n"] == result["post"]["n"] == 150
    assert result["pre"]["runs"][0]["run_id"] == 151
    assert result["post"]["runs"][0]["run_id"] == 152
    assert result["post"]["runs"][-1]["run_id"] == 301


@pytest.mark.parametrize("defect", ["not_contributed", "silence_killed", "late"])
def test_red_recurrence(db, defect):
    path, conn = db
    populate(conn)
    if defect == "not_contributed":
        conn.execute("DELETE FROM task_comments WHERE task_id IN (SELECT task_id FROM task_runs WHERE id>180)")
    elif defect == "silence_killed":
        conn.execute("UPDATE task_runs SET outcome='silence_killed' WHERE id=200")
    else:
        conn.execute("UPDATE task_comments SET created_at=created_at+500 WHERE task_id IN "
                     "(SELECT task_id FROM task_runs WHERE id>170)")
    result = report(db)
    assert result["verdict"] == "FAIL"
    assert result["exit_code"] == 1
    assert "RED:" in gate.render(result)


def test_read_only_cli_json_and_capped_inspection(db):
    path, conn = db
    for i in range(30):
        rid = run(conn, E + i)
        comment(conn, rid, E + i + 1)
    conn.commit()
    before = path.read_bytes()
    before_names = sorted(p.name for p in path.parent.iterdir())
    result = subprocess.run([sys.executable, str(SCRIPT), "--db", str(path), "--landing-epoch", str(D),
                             "--deploy-epoch", str(E), "--json"], capture_output=True, text=True)
    data = json.loads(result.stdout)
    assert result.returncode == 2
    assert len(data["intent_only_comments"]) == 20
    assert path.read_bytes() == before
    assert sorted(p.name for p in path.parent.iterdir()) == before_names
    with pytest.raises(sqlite3.OperationalError):
        gate.measure(path.parent / "nonexistent.sqlite", D, E)
    assert not (path.parent / "nonexistent.sqlite").exists()
