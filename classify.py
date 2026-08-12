#!/usr/bin/env python3
"""
classify.py -- constrained LLM classification for jt.

The model proposes. Deterministic code commits. Model output is untrusted input
and is validated like a form submission from a stranger.
"""

import argparse
import contextlib
import fcntl
import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import jt
import transitions

DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_BATCH_SIZE = 10
DEFAULT_MIN_CONFIDENCE = 0.85
MAX_MODEL_ATTEMPTS = 5
COMPANY_MIN_SCORE = 88.0
ROLE_MIN_SCORE = 80.0
RUNNER_UP_GAP = 10.0
MAX_EVIDENCE_WORDS = 14
MIN_EVIDENCE_WORDS = 3


@dataclass(frozen=True)
class Verdict:
    gmail_msg_id: str
    ok: bool
    reason: str
    proposal: dict[str, Any] | None = None
    application_id: int | None = None


def _ratio(a: str, b: str) -> float:
    try:
        from rapidfuzz import fuzz
    except ImportError:
        return SequenceMatcher(None, a.lower(), b.lower()).ratio() * 100
    return float(fuzz.ratio(a, b))


def _min_confidence() -> float:
    try:
        return float(os.environ.get("CLASSIFIER_MIN_CONFIDENCE", DEFAULT_MIN_CONFIDENCE))
    except ValueError:
        raise SystemExit("error: CLASSIFIER_MIN_CONFIDENCE must be a number")


def _batch_size() -> int:
    try:
        return int(os.environ.get("CLASSIFIER_BATCH_SIZE", DEFAULT_BATCH_SIZE))
    except ValueError:
        raise SystemExit("error: CLASSIFIER_BATCH_SIZE must be an integer")


def _status_code(exc: Exception) -> int | None:
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "resp", None), "status", None)
    return int(status) if status is not None else None


def _retryable(exc: Exception) -> bool:
    return _status_code(exc) in {408, 409, 429, 500, 502, 503, 504}


@contextlib.contextmanager
def classifier_lock(path: Path | None = None):
    """One classifier process at a time per database.

    The lock is advisory and process-scoped. If the process dies, the OS releases
    it, so stale lock files are harmless.
    """
    lock_path = path or jt.db_path().with_suffix(jt.db_path().suffix + ".classify.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("error: another jt-classify process is already running")
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def load_unprocessed(conn, limit: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT * FROM raw_messages
           WHERE processed = 0
           ORDER BY received_on, gmail_msg_id
           LIMIT ?""",
        (limit,),
    ).fetchall()


def prompt_for(messages: list[sqlite3.Row]) -> str:
    items = []
    for m in messages:
        body = (m["body"] or "")[:2500]
        items.append({
            "gmail_msg_id": m["gmail_msg_id"],
            "received_on": m["received_on"],
            "sender": m["sender"],
            "subject": m["subject"],
            "body": body,
        })
    return (
        "Classify job application emails. Return only a JSON array, no prose, "
        "no markdown fences. Each item must contain gmail_msg_id, company, "
        "role_hint, kind, occurred_on, confidence, evidence. "
        f"kind must be one of: {', '.join(jt.EVENT_KINDS)}. "
        "evidence must be a verbatim span from the email body under 15 words. "
        "If there is no clear application event, omit that message.\n\n"
        + json.dumps(items, ensure_ascii=False)
    )


def build_classifier_client():
    try:
        import anthropic
    except ImportError as e:
        raise SystemExit(
            "error: classifier requires the classify extra:\n"
            "  pipx install -e '.[classify]'\n"
            f"missing import: {e.name}"
        ) from e
    return anthropic.Anthropic()


def call_model(client, messages: list[sqlite3.Row], sleep=time.sleep) -> str:
    model = os.environ.get("CLASSIFIER_MODEL", DEFAULT_MODEL)
    delay = 1.0
    for attempt in range(1, MAX_MODEL_ATTEMPTS + 1):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=3000,
                messages=[{"role": "user", "content": prompt_for(messages)}],
            )
            return response.content[0].text
        except Exception as e:
            if not _retryable(e) or attempt == MAX_MODEL_ATTEMPTS:
                raise
            sleep(delay)
            delay *= 2


def parse_json_array(raw: str) -> tuple[list[dict[str, Any]] | None, str]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return None, f"malformed JSON from classifier: {e.msg}"
    if not isinstance(data, list):
        return None, "classifier returned JSON, but not an array"
    if not all(isinstance(x, dict) for x in data):
        return None, "classifier array contained a non-object item"
    return data, ""


def _word_count(s: str) -> int:
    return len([w for w in s.split() if w.strip()])


def _field_types(p: dict[str, Any]) -> str:
    required = {
        "gmail_msg_id": str,
        "company": str,
        "role_hint": str,
        "kind": str,
        "occurred_on": str,
        "confidence": (int, float),
        "evidence": str,
    }
    for key, typ in required.items():
        if key not in p:
            return f"missing field {key!r}"
        if not isinstance(p[key], typ):
            return f"field {key!r} has wrong type"
    return ""


def _date_reason(s: str) -> str:
    try:
        date.fromisoformat(s)
    except ValueError:
        return f"occurred_on is not YYYY-MM-DD: {s!r}"
    return ""


def _message_map(messages: list[sqlite3.Row]) -> dict[str, sqlite3.Row]:
    return {m["gmail_msg_id"]: m for m in messages}


def _app_score(app, company: str, role_hint: str) -> tuple[float, float]:
    return _ratio(company, app["company"]), _ratio(role_hint or "", app["role"])


def match_application(conn, company: str, role_hint: str) -> tuple[sqlite3.Row | None, str]:
    apps = conn.execute("SELECT * FROM applications ORDER BY id").fetchall()
    if not apps:
        return None, "no applications exist to match against"

    scored = [(app, *_app_score(app, company, role_hint)) for app in apps]
    scored.sort(key=lambda x: x[1], reverse=True)
    top_company = scored[0][1]
    if top_company < COMPANY_MIN_SCORE:
        return None, f"no application company match for {company!r}"
    near_company = [x for x in scored if x[1] >= top_company - RUNNER_UP_GAP]
    near_names = {x[0]["company"].strip().lower() for x in near_company}
    if len(near_names) > 1:
        return None, f"ambiguous company match for {company!r}"

    same_company = near_company
    if len(same_company) == 1:
        return same_company[0][0], ""

    if not role_hint.strip():
        return None, f"ambiguous role at {same_company[0][0]['company']}; no role_hint"
    same_company.sort(key=lambda x: x[2], reverse=True)
    top_role = same_company[0][2]
    runner_role = same_company[1][2] if len(same_company) > 1 else -1.0
    if top_role < ROLE_MIN_SCORE:
        return None, f"ambiguous role for {company!r}; role_hint did not match"
    if runner_role >= top_role - RUNNER_UP_GAP:
        return None, f"ambiguous role for {company!r}; multiple roles matched"
    return same_company[0][0], ""


def validate_proposal(conn, p: dict[str, Any], messages_by_id: dict[str, sqlite3.Row]) -> Verdict:
    type_reason = _field_types(p)
    msg_id = p.get("gmail_msg_id") if isinstance(p.get("gmail_msg_id"), str) else "unknown"
    if type_reason:
        return Verdict(msg_id, False, type_reason, p)

    msg = messages_by_id.get(p["gmail_msg_id"])
    if msg is None:
        return Verdict(p["gmail_msg_id"], False, "proposal referenced an unknown gmail_msg_id", p)
    if p["kind"] == "note":
        return Verdict(p["gmail_msg_id"], False, "classifier may not write human note events", p)
    if p["kind"] not in jt.EVENT_KINDS:
        return Verdict(p["gmail_msg_id"], False, f"unknown event kind {p['kind']!r}", p)
    date_reason = _date_reason(p["occurred_on"])
    if date_reason:
        return Verdict(p["gmail_msg_id"], False, date_reason, p)
    if not (0.0 <= float(p["confidence"]) <= 1.0):
        return Verdict(p["gmail_msg_id"], False, "confidence must be between 0.0 and 1.0", p)
    if float(p["confidence"]) < _min_confidence():
        return Verdict(p["gmail_msg_id"], False, "confidence below threshold", p)

    evidence = p["evidence"].strip()
    words = _word_count(evidence)
    if words > MAX_EVIDENCE_WORDS:
        return Verdict(p["gmail_msg_id"], False, "evidence span is too long", p)
    if words < MIN_EVIDENCE_WORDS:
        return Verdict(p["gmail_msg_id"], False, "evidence span is too short to be diagnostic", p)
    if evidence not in (msg["body"] or ""):
        return Verdict(p["gmail_msg_id"], False, "evidence is not a verbatim span from body", p)

    app, reason = match_application(conn, p["company"], p["role_hint"])
    if reason:
        return Verdict(p["gmail_msg_id"], False, reason, p)
    if p["occurred_on"] < app["applied_on"]:
        return Verdict(
            p["gmail_msg_id"], False,
            f"event date {p['occurred_on']} predates application date {app['applied_on']}", p)

    current = jt.derive(conn, app)["status"]
    ok, reason = transitions.is_legal(current, p["kind"])
    if not ok:
        return Verdict(p["gmail_msg_id"], False, reason, p)
    return Verdict(p["gmail_msg_id"], True, "", p, app["id"])


def verdicts_for(conn, messages: list[sqlite3.Row], raw_response: str) -> list[Verdict]:
    proposals, reason = parse_json_array(raw_response)
    if proposals is None:
        return [Verdict(m["gmail_msg_id"], False, reason, {"raw": raw_response}) for m in messages]

    counts: dict[str, int] = {}
    for p in proposals:
        msg_id = p.get("gmail_msg_id")
        if isinstance(msg_id, str):
            counts[msg_id] = counts.get(msg_id, 0) + 1

    messages_by_id = _message_map(messages)
    verdicts: list[Verdict] = []
    seen = set()
    for p in proposals:
        msg_id = p.get("gmail_msg_id")
        if isinstance(msg_id, str):
            seen.add(msg_id)
        if isinstance(msg_id, str) and counts.get(msg_id, 0) > 1:
            verdicts.append(Verdict(
                msg_id, False, "one Gmail message proposed multiple events; human review required", p))
            continue
        verdicts.append(validate_proposal(conn, p, messages_by_id))

    for m in messages:
        if m["gmail_msg_id"] not in seen:
            verdicts.append(Verdict(
                m["gmail_msg_id"], False, "classifier returned no proposal for this message", None))
    return verdicts


def write_review(conn, verdict: Verdict) -> None:
    conn.execute(
        """INSERT INTO review_queue (gmail_msg_id, proposed_json, reason)
           VALUES (?,?,?)""",
        (verdict.gmail_msg_id, json.dumps(verdict.proposal, sort_keys=True), verdict.reason),
    )


def commit_verdict(conn, verdict: Verdict) -> str:
    assert verdict.proposal is not None and verdict.application_id is not None
    p = verdict.proposal
    try:
        cur = conn.execute(
            """INSERT OR IGNORE INTO events
               (application_id, occurred_on, kind, gmail_msg_id, confidence, evidence)
               VALUES (?,?,?,?,?,?)""",
            (verdict.application_id, p["occurred_on"], p["kind"], p["gmail_msg_id"],
             float(p["confidence"]), p["evidence"]),
        )
    except sqlite3.IntegrityError as e:
        return f"database rejected event: {e}"
    if cur.rowcount == 0:
        return "duplicate gmail_msg_id already exists in events; no event inserted"
    return ""


def apply_verdicts(conn, verdicts: list[Verdict]) -> tuple[int, int]:
    committed = reviewed = 0
    for verdict in verdicts:
        if verdict.ok:
            reason = commit_verdict(conn, verdict)
            if reason:
                write_review(conn, Verdict(
                    verdict.gmail_msg_id, False, reason, verdict.proposal))
                reviewed += 1
            else:
                committed += 1
        else:
            write_review(conn, verdict)
            reviewed += 1
        conn.execute(
            "UPDATE raw_messages SET processed = 1 WHERE gmail_msg_id = ?",
            (verdict.gmail_msg_id,),
        )
    conn.commit()
    return committed, reviewed


def _process_batch_unlocked(conn, client, *, dry_run: bool = False) -> tuple[int, int, int]:
    messages = load_unprocessed(conn, _batch_size())
    if not messages:
        return 0, 0, 0
    raw_response = call_model(client, messages)
    verdicts = verdicts_for(conn, messages, raw_response)
    if dry_run:
        for v in verdicts:
            status = "commit" if v.ok else "review"
            print(f"{v.gmail_msg_id}: {status}" + (f" -- {v.reason}" if v.reason else ""))
        return len(messages), sum(1 for v in verdicts if v.ok), sum(1 for v in verdicts if not v.ok)
    committed, reviewed = apply_verdicts(conn, verdicts)
    return len(messages), committed, reviewed


def process_batch(conn, client, *, dry_run: bool = False) -> tuple[int, int, int]:
    if dry_run:
        return _process_batch_unlocked(conn, client, dry_run=True)
    with classifier_lock():
        return _process_batch_unlocked(conn, client, dry_run=False)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="classify raw Gmail messages into jt events")
    p.add_argument("--dry-run", action="store_true")
    return p


def main() -> None:
    args = build_parser().parse_args()
    conn = jt.connect()
    try:
        seen, committed, reviewed = process_batch(
            conn, build_classifier_client(), dry_run=args.dry_run)
    finally:
        conn.close()
    print(f"processed {seen}, committed {committed}, review {reviewed}")


if __name__ == "__main__":
    main()
