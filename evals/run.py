#!/usr/bin/env python3
"""
Run the redacted classifier eval set.

By default this calls the configured classifier model, then runs the exact same
validation path as classify.py. `--use-expected` is for testing the evaluator
itself without live API credentials.
"""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import classify
import jt


def load_fixtures(path: Path) -> list[dict]:
    fixtures = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                fixtures.append(json.loads(line))
    return fixtures


def _insert_app(conn, company: str, role: str = "Backend Engineer") -> None:
    conn.execute(
        "INSERT OR IGNORE INTO applications (company, role, lane, applied_on) VALUES (?,?,?,?)",
        (company, role, "swe", "2026-08-01"),
    )


def seed_db(conn, fixtures: list[dict]) -> None:
    for fx in fixtures:
        expect = fx.get("expect") or {}
        company = expect.get("company")
        if not company or not expect.get("should_commit"):
            continue
        if "ambiguous" in (fx.get("note") or "").lower():
            _insert_app(conn, company, "Software Engineer")
            _insert_app(conn, company, "Platform Engineer")
        else:
            _insert_app(conn, company)
    for fx in fixtures:
        conn.execute(
            """INSERT INTO raw_messages
               (gmail_msg_id, received_on, sender, subject, body, processed)
               VALUES (?,?,?,?,?,0)""",
            (fx["id"], "2026-08-02", fx.get("sender"), fx.get("subject"), fx.get("body", "")),
        )
    conn.commit()


def expected_response(fixtures: list[dict]) -> str:
    proposals = []
    for fx in fixtures:
        expect = fx.get("expect") or {}
        kind = expect.get("kind")
        if not kind:
            continue
        body = fx.get("body", "")
        evidence = " ".join(body.split()[: min(8, len(body.split()))])
        proposals.append({
            "gmail_msg_id": fx["id"],
            "company": expect.get("company") or "",
            "role_hint": "Backend Engineer",
            "kind": kind,
            "occurred_on": "2026-08-02",
            "confidence": 0.99,
            "evidence": evidence,
        })
    return json.dumps(proposals)


def run_eval(fixtures: list[dict], *, use_expected: bool = False, client=None) -> dict:
    with tempfile.TemporaryDirectory() as d:
        old = os.environ.get("JT_DB")
        os.environ["JT_DB"] = str(Path(d) / "eval.db")
        try:
            conn = jt.connect()
            seed_db(conn, fixtures)
            messages = classify.load_unprocessed(conn, len(fixtures))
            raw = expected_response(fixtures) if use_expected else classify.call_model(
                client or classify.build_classifier_client(), messages)
            verdicts = classify.verdicts_for(conn, messages, raw)
        finally:
            if "conn" in locals():
                conn.close()
            if old is None:
                os.environ.pop("JT_DB", None)
            else:
                os.environ["JT_DB"] = old

    expected = {fx["id"]: (fx.get("expect") or {}) for fx in fixtures}
    actual_commits = {v.gmail_msg_id for v in verdicts if v.ok}
    expected_commits = {
        msg_id for msg_id, exp in expected.items() if exp.get("should_commit")
    }
    true_commits = actual_commits & expected_commits
    false_commits = actual_commits - expected_commits

    precision = len(true_commits) / len(actual_commits) if actual_commits else 1.0
    recall = len(true_commits) / len(expected_commits) if expected_commits else 1.0
    return {
        "fixtures": len(fixtures),
        "expected_commits": len(expected_commits),
        "actual_commits": len(actual_commits),
        "precision": precision,
        "recall": recall,
        "misclassifications_reached_events": len(false_commits),
        "reviewed": len([v for v in verdicts if not v.ok]),
    }


def print_report(report: dict) -> None:
    print(f"fixtures: {report['fixtures']}")
    print(f"expected commits: {report['expected_commits']}")
    print(f"actual commits: {report['actual_commits']}")
    print(f"precision: {report['precision']:.2%}")
    print(f"recall: {report['recall']:.2%}")
    print(f"reviewed: {report['reviewed']}")
    print()
    print(
        "MISCLASSIFICATIONS THAT REACHED EVENTS: "
        f"{report['misclassifications_reached_events']}"
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="run jt classifier evals")
    p.add_argument("fixtures", nargs="?", default="evals/fixtures.jsonl")
    p.add_argument("--use-expected", action="store_true",
                   help="test the eval harness without calling the model")
    return p


def main() -> None:
    args = build_parser().parse_args()
    path = Path(args.fixtures)
    if not path.exists():
        raise SystemExit(
            f"error: {path} does not exist. Copy/redact fixtures from "
            "evals/fixtures.example.jsonl first."
        )
    print_report(run_eval(load_fixtures(path), use_expected=args.use_expected))


if __name__ == "__main__":
    main()
