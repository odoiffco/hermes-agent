#!/usr/bin/env python3
"""Read-only first-contribution observation gate; never infer a deployment epoch.

Outcome diagnostics cover all runs in each temporal window; included counts
cover only the fixed-size selected cohort. Ties are resolved by run id.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sqlite3
import statistics

INCLUDED = (
    "completed", "review_requested", "blocked", "changes_requested",
    "timed_out", "silence_killed", "gave_up", "rate_limited",
)
EXCLUDED = ("crashed", "spawn_failed", "reclaimed", "secret_hydration_unavailable", "scheduled")
INTENT_OUTCOMES = ("completed", "review_requested", "blocked")


def classify_run(conn, run):
    start, end = run["started_at"], run["ended_at"]
    args = (run["task_id"], run["profile"], start, end)
    comments = [dict(row) for row in conn.execute(
        "SELECT body, created_at FROM task_comments WHERE task_id=? AND author=? "
        "AND created_at BETWEEN ? AND ? AND length(body)>=120 ORDER BY created_at, id", args
    )]
    attachments = [row[0] for row in conn.execute(
        "SELECT created_at FROM task_attachments WHERE task_id=? AND uploaded_by=? "
        "AND created_at BETWEEN ? AND ? ORDER BY created_at, id", args
    )]
    summary_qualifies = len(run["summary"] or "") >= 120
    intent_only = bool(comments) and all(c["created_at"] <= start + 600 for c in comments) \
        and not attachments and not summary_qualifies and run["outcome"] in INTENT_OUTCOMES
    times = attachments + [c["created_at"] for c in comments]
    if summary_qualifies:
        times.append(end)
    ttc = min(times) - start if times and not intent_only else None
    return {
        "run_id": run["id"], "task_id": run["task_id"], "outcome": run["outcome"],
        "ttc_seconds": ttc, "intent_only": intent_only,
        "declaration_count": sum(c["created_at"] <= start + 600 for c in comments),
        "intent_only_comments": comments if intent_only else [],
    }


def cohort(conn, predicate, epoch, size, order):
    if epoch is None:
        window = []
        runs = []
    else:
        window = conn.execute(
            f"SELECT outcome, ended_at FROM task_runs WHERE {predicate}", (epoch,)
        ).fetchall()
        slots = ",".join("?" for _ in INCLUDED)
        runs = conn.execute(
            f"SELECT * FROM task_runs WHERE {predicate} AND ended_at IS NOT NULL "
            f"AND outcome IN ({slots}) ORDER BY {order} LIMIT ?",
            (epoch, *INCLUDED, size),
        ).fetchall()
    selected = [classify_run(conn, row) for row in runs]
    excluded_counts = Counter()
    unknown_counts = Counter()
    for row in window:
        outcome = row["outcome"]
        if outcome not in INCLUDED or row["ended_at"] is None:
            excluded_counts[outcome or "<NULL/EMPTY>"] += 1
        if outcome and outcome not in INCLUDED and outcome not in EXCLUDED:
            unknown_counts[outcome] += 1
    n = len(selected)
    ttcs = [r["ttc_seconds"] for r in selected if r["ttc_seconds"] is not None]
    missing = n - len(ttcs)
    late = missing + sum(t > 600 for t in ttcs)
    kills = sum(r["outcome"] == "silence_killed" for r in selected)
    return {
        "n": n, "included_counts": dict(sorted(Counter(r["outcome"] for r in selected).items())),
        "excluded_counts": dict(sorted(excluded_counts.items())),
        "unknown_outcomes": dict(sorted(unknown_counts.items())),
        "not_contributed_count": missing, "not_contributed_share": missing / n if n else None,
        "late_or_never_count": late, "late_or_never_share": late / n if n else None,
        "median_ttc_seconds": statistics.median(ttcs) if ttcs else None,
        "silence_killed_count": kills, "silence_killed_per_100": kills * 100 / n if n else None,
        "runs": selected,
    }


def measure(db, landing_epoch, deploy_epoch, pre_n=150, post_n=150, min_n=100):
    # URI quoting prevents '?' and '#' in file names from changing the connection mode.
    uri = Path(db).expanduser().resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN")  # One consistent read snapshot, including all artifact queries.
        pre = cohort(conn, "ended_at < ?", landing_epoch, pre_n, "ended_at DESC, id DESC")
        post = cohort(conn, "started_at >= ?", deploy_epoch, post_n, "started_at ASC, id ASC")
        unknown = dict(sorted((row[0], row[1]) for row in conn.execute(
            "SELECT outcome, count(*) FROM task_runs WHERE outcome IS NOT NULL AND outcome != '' "
            f"AND outcome NOT IN ({','.join('?' for _ in INCLUDED + EXCLUDED)}) GROUP BY outcome",
            INCLUDED + EXCLUDED,
        )))
    finally:
        conn.close()
    minimum = max(100, min_n)
    reasons = []
    if deploy_epoch is None or pre["n"] < minimum or post["n"] < minimum:
        verdict, code = "INSUFFICIENT DATA", 2
        reasons.append(f"Need known deploy epoch and >= {minimum} runs per cohort; "
                       f"E={deploy_epoch}, pre n={pre['n']}, post n={post['n']}.")
    else:
        if post["not_contributed_share"] >= .15:
            reasons.append("post not-contributed share >= 15%" +
                           (" (defect recurrence: >= 20%)" if post["not_contributed_share"] >= .20 else ""))
        if post["late_or_never_share"] > .25:
            reasons.append("post late-or-never share > 25%")
        if pre["median_ttc_seconds"] is None or post["median_ttc_seconds"] is None \
                or post["median_ttc_seconds"] >= pre["median_ttc_seconds"]:
            reasons.append("median TTC did not strictly fall (or is undefined)")
        if post["silence_killed_per_100"] > pre["silence_killed_per_100"]:
            reasons.append("silence_killed per 100 increased (defect recurrence)")
        verdict, code = ("FAIL", 1) if reasons else ("PASS", 0)
    inspections = []
    for name, data in (("pre", pre), ("post", post)):
        for run in data["runs"]:
            for comment in run["intent_only_comments"]:
                if len(inspections) < 20:
                    inspections.append({"cohort": name, "run_id": run["run_id"], **comment})
    # Full inspection bodies occur only in the capped list, not duplicated per run.
    for data in (pre, post):
        for run in data["runs"]:
            del run["intent_only_comments"]
    return {
        "landing_epoch": landing_epoch, "deploy_epoch": deploy_epoch,
        "pre_n": pre_n, "post_n": post_n, "min_n": minimum,
        "outcome_count_scope": "included: selected cohort; excluded: entire temporal window; unknown: entire DB",
        "pre": pre, "post": post, "unknown_outcomes": unknown,
        "intent_only_comments": inspections, "reasons": reasons, "verdict": verdict, "exit_code": code,
    }


def render(report):
    lines = [f"First contribution gate: D={report['landing_epoch']} E={report['deploy_epoch']}",
             report["outcome_count_scope"]]
    for name in ("pre", "post"):
        data = report[name]
        lines.append(f"{name}: n={data['n']}")
        for key in ("included_counts", "excluded_counts", "not_contributed_share", "late_or_never_share",
                    "median_ttc_seconds", "silence_killed_count", "silence_killed_per_100"):
            lines.append(f"  {key}: {json.dumps(data[key], sort_keys=True)}")
    lines.append("UNKNOWN-OUTCOME: " + json.dumps(report["unknown_outcomes"], sort_keys=True))
    for comment in report["intent_only_comments"]:
        lines.append(f"INTENT-ONLY {comment['cohort']} run {comment['run_id']} at {comment['created_at']}:\n{comment['body']}")
    prefix = "RED: " if report["verdict"] == "FAIL" else "DATA: "
    lines.extend(prefix + reason for reason in report["reasons"])
    lines.append("VERDICT: " + report["verdict"])
    return "\n".join(lines)


def positive_int(value):
    result = int(value)
    if result < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--landing-epoch", type=int, required=True)
    parser.add_argument("--deploy-epoch", type=int, default=None, help="omit when deployment is unknown")
    parser.add_argument("--pre-n", type=positive_int, default=150)
    parser.add_argument("--post-n", type=positive_int, default=150)
    parser.add_argument("--min-n", type=positive_int, default=100)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.deploy_epoch is not None and args.deploy_epoch < args.landing_epoch:
        parser.error("deploy epoch must not precede landing epoch")
    report = measure(args.db, args.landing_epoch, args.deploy_epoch, args.pre_n, args.post_n, args.min_n)
    print(json.dumps(report, indent=2, ensure_ascii=False) if args.json else render(report))
    return report["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
