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
from datetime import date, datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import jt
import transitions

# Unversioned alias: always resolves to Anthropic's current Sonnet snapshot.
# Convenient default, but it means behavior can shift under you without a
# code change -- anyone scoring against evals/fixtures.jsonl for real should
# override CLASSIFIER_MODEL with an exact dated snapshot instead. See
# .env.example and docs/automation.md for the alias-vs-pin tradeoff.
DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_BATCH_SIZE = 10
DEFAULT_MIN_CONFIDENCE = 0.85
MAX_MODEL_ATTEMPTS = 5
COMPANY_MIN_SCORE = 88.0
ROLE_MIN_SCORE = 80.0
RUNNER_UP_GAP = 10.0
MAX_EVIDENCE_WORDS = 14
MIN_EVIDENCE_WORDS = 3
# Overridable so a second machine, a different account, or CI doesn't need
# this literal home directory to exist. Mirrors jt.py's db_path() pattern.
LOG_PATH = Path(os.environ.get("JT_LOG_DIR", str(Path.home() / ".jobtrack" / "logs"))) / "jt-classify.jsonl"


def selected_model() -> str:
    return os.environ.get("CLASSIFIER_MODEL", DEFAULT_MODEL)


TOOL_NAME = "propose_events"

# Schema-typed at the API boundary: the model is forced (tool_choice, see
# _call_model_result) to call this tool, so its output arrives as a
# JSON-schema-conformant object, not free text we then have to parse and
# hope is valid JSON.
#
# What this eliminates, at the API boundary, before any Python runs:
#   - malformed/non-JSON output (no more "malformed JSON from classifier")
#   - a non-array top-level response
#   - a non-object item in the array
#   - a missing or wrong-typed required field
#   - a kind string outside the enum (and "note" is excluded from the enum
#     entirely -- the model cannot even attempt to write a human note event,
#     compared to before, where that was only caught after the fact)
#
# What this does NOT and CANNOT eliminate, because JSON Schema has no way
# to express it -- validate_proposal() in this file remains the actual
# authority for all of it, unchanged:
#   - evidence being a real verbatim span copied from that specific
#     message's body (schema can bound string length, not cross-reference
#     a different field's content)
#   - occurred_on being a real calendar date, not just YYYY-MM-DD-shaped
#     (a "pattern" match is not a real date; 2026-13-45 matches the regex)
#   - confidence meeting the configured threshold (schema bounds 0.0-1.0,
#     but CLASSIFIER_MIN_CONFIDENCE is a runtime setting, not a schema fact)
#   - company/role matching a real application in the database
#   - the proposed transition being legal from the application's current,
#     derived status
#   - two proposals colliding on the same gmail_msg_id
# In short: schema-typing narrows what a malformed *response* can look
# like. It says nothing about whether a well-formed response is *true*.
# That distinction is why validate_proposal()'s checks all stay, even
# though several of them (field presence/type, kind membership, confidence
# bounds) are now redundant in the common case -- "redundant" is not the
# same as "safe to remove": constrained decoding is Anthropic's
# implementation detail, not a documented contractual guarantee, and this
# file's whole thesis is that model output is untrusted regardless.
def classification_tool_schema() -> dict:
    # "note" is deliberately excluded -- see transitions.py: a note is a
    # human act, and the classifier must not be able to write one even by
    # accident. Excluding it from the enum stops that at generation time
    # instead of relying solely on validate_proposal()'s runtime check.
    allowed_kinds = [k for k in jt.EVENT_KINDS if k != "note"]
    return {
        "name": TOOL_NAME,
        "description": (
            "Propose job-application events extracted from a batch of "
            "emails. Include a proposal only for a message with a clear "
            "application event; omit any message that is ambiguous or "
            "unrelated rather than guessing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "proposals": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "gmail_msg_id": {
                                "type": "string",
                                "description": (
                                    "Must exactly match a gmail_msg_id "
                                    "from the input batch."
                                ),
                            },
                            "company": {"type": "string"},
                            "role_hint": {
                                "type": "string",
                                "description": (
                                    "Role/title mentioned in the email, "
                                    "or an empty string if unclear."
                                ),
                            },
                            "kind": {
                                "type": "string",
                                "enum": allowed_kinds,
                            },
                            "occurred_on": {
                                "type": "string",
                                "pattern": r"^\d{4}-\d{2}-\d{2}$",
                                "description": "Date the event happened, YYYY-MM-DD.",
                            },
                            "confidence": {
                                "type": "number",
                                "minimum": 0.0,
                                "maximum": 1.0,
                            },
                            "evidence": {
                                "type": "string",
                                "description": (
                                    "A VERBATIM span copied exactly from "
                                    "that message's body, under 15 words. "
                                    "Not paraphrased or summarized."
                                ),
                            },
                        },
                        "required": [
                            "gmail_msg_id", "company", "role_hint", "kind",
                            "occurred_on", "confidence", "evidence",
                        ],
                    },
                },
            },
            "required": ["proposals"],
        },
    }


@dataclass(frozen=True)
class ModelCallStats:
    attempt_count: int
    status_code: int | None
    error_type: str | None
    latency_ms: int
    retryable_failures_recovered: bool = False
    # Message count in the batch this call was for. Populated on both
    # success and failure -- on a failed/exhausted call it's the only way a
    # log line says how many raw_messages are sitting stuck, which is what
    # turns "Anthropic down" from a severity guess into a scoped one.
    batch_size: int = 0
    # None on any failed call (Anthropic doesn't return usage for a call
    # that never completed). Present on success so per-run cost is
    # computable from the log without re-deriving it from Anthropic's own
    # billing console after the fact.
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True)
class ModelCallResult:
    text: str
    stats: ModelCallStats


@dataclass(frozen=True)
class ClassificationResult:
    seen: int
    committed: int
    reviewed: int
    model_stats: ModelCallStats | None = None
    malformed_output: bool = False


class ModelCallError(Exception):
    def __init__(self, original: Exception, stats: ModelCallStats, *, exhausted: bool):
        super().__init__(str(original))
        self.original = original
        self.stats = stats
        self.exhausted = exhausted


@dataclass(frozen=True)
class Verdict:
    gmail_msg_id: str
    ok: bool
    reason: str
    proposal: dict[str, Any] | None = None
    application_id: int | None = None


def _structured_log(event: str, **fields: Any) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "component": "jt-classify",
        "schema_version": CLASSIFY_LOG_SCHEMA_VERSION,
        "event": event,
        **fields,
    }
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, sort_keys=True) + "\n")


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
    # Shape (required fields, kind enum, types) is now owned entirely by
    # classification_tool_schema() -- tool_choice forces the call, so
    # repeating "return only a JSON array" here would be talking to a
    # constraint that no longer exists. What's left is exactly the
    # semantic guidance the schema has no way to express: verbatim-ness
    # and when to omit a message. Keep both in sync with
    # classification_tool_schema()'s own property descriptions if either
    # changes.
    return (
        "Classify job application emails using the propose_events tool. "
        "evidence must be copied verbatim from that message's body, under "
        "15 words -- not paraphrased. Include a proposal only for a "
        "message with a clear application event; omit any message that is "
        "ambiguous or unrelated rather than guessing.\n\n"
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


def _usage_tokens(response: Any) -> tuple[int | None, int | None]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None, None
    return getattr(usage, "input_tokens", None), getattr(usage, "output_tokens", None)


def _proposals_raw_text(response: Any) -> str:
    """Extract the tool call's `proposals` array as a JSON-array string.

    Re-serializing back to text (rather than threading the parsed dict
    through) is deliberate: it keeps this function the only thing that
    knows the API boundary changed. Everything downstream --
    parse_json_array(), verdicts_for(), validate_proposal() -- still takes
    the exact same "raw JSON-array string" contract it always did, so none
    of the actual safety validation had to change to gain schema-typing.

    tool_choice forces this specific tool, so a missing tool_use block
    should be unreachable in practice (the one plausible cause is the
    response getting cut off before the tool call completed, e.g. hitting
    max_tokens mid-call). Rather than crash, fall back to "" -- an empty
    string is invalid JSON, so parse_json_array() reports it as malformed
    and every message in the batch routes to review. Silent-but-wrong is
    worse than loud-but-routed-to-review.
    """
    for block in getattr(response, "content", None) or []:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == TOOL_NAME:
            tool_input = getattr(block, "input", None)
            proposals = tool_input.get("proposals") if isinstance(tool_input, dict) else None
            if isinstance(proposals, list):
                return json.dumps(proposals)
    return ""


def _call_model_result(client, messages: list[sqlite3.Row], sleep=time.sleep) -> ModelCallResult:
    model = selected_model()
    batch_size = len(messages)
    delay = 1.0
    started = time.monotonic()
    last_status_code = None
    last_error_type = None
    for attempt in range(1, MAX_MODEL_ATTEMPTS + 1):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=3000,
                tools=[classification_tool_schema()],
                tool_choice={"type": "tool", "name": TOOL_NAME},
                messages=[{"role": "user", "content": prompt_for(messages)}],
            )
            latency_ms = int((time.monotonic() - started) * 1000)
            input_tokens, output_tokens = _usage_tokens(response)
            return ModelCallResult(
                _proposals_raw_text(response),
                ModelCallStats(
                    attempt_count=attempt,
                    status_code=last_status_code,
                    error_type=last_error_type,
                    latency_ms=latency_ms,
                    retryable_failures_recovered=attempt > 1,
                    batch_size=batch_size,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                ),
            )
        except Exception as e:
            last_status_code = _status_code(e)
            last_error_type = type(e).__name__
            exhausted = _retryable(e) and attempt == MAX_MODEL_ATTEMPTS
            if not _retryable(e) or exhausted:
                latency_ms = int((time.monotonic() - started) * 1000)
                raise ModelCallError(
                    e,
                    ModelCallStats(
                        attempt_count=attempt,
                        status_code=last_status_code,
                        error_type=last_error_type,
                        latency_ms=latency_ms,
                        batch_size=batch_size,
                    ),
                    exhausted=exhausted,
                ) from e
            sleep(delay)
            delay *= 2


def call_model(client, messages: list[sqlite3.Row], sleep=time.sleep) -> str:
    try:
        return _call_model_result(client, messages, sleep=sleep).text
    except ModelCallError as e:
        raise e.original from e


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


def _routes_to_application_candidate(verdict: Verdict) -> bool:
    if verdict.proposal is None:
        return False
    return (
        verdict.reason == "no applications exist to match against"
        or verdict.reason.startswith("no application company match")
    )


def write_application_candidate(conn, verdict: Verdict) -> None:
    assert verdict.proposal is not None
    p = verdict.proposal
    jt.insert_application_candidate(
        conn,
        raw_input=json.dumps(p, sort_keys=True),
        source="classifier",
        source_ref=f"gmail:{verdict.gmail_msg_id}",
        company=p.get("company") if isinstance(p.get("company"), str) else None,
        role=p.get("role_hint") if isinstance(p.get("role_hint"), str) else None,
        applied_on=p.get("occurred_on") if isinstance(p.get("occurred_on"), str) else None,
        notes=(
            "classifier proposed "
            f"{p.get('kind')!r}; routed to candidates because {verdict.reason}; "
            f"evidence={p.get('evidence')!r}"
        ),
        commit=False,
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
            if _routes_to_application_candidate(verdict):
                write_application_candidate(conn, verdict)
            else:
                write_review(conn, verdict)
            reviewed += 1
        conn.execute(
            "UPDATE raw_messages SET processed = 1 WHERE gmail_msg_id = ?",
            (verdict.gmail_msg_id,),
        )
    conn.commit()
    return committed, reviewed


def _malformed_output(verdicts: list[Verdict]) -> bool:
    return any(
        v.reason.startswith((
            "malformed JSON from classifier",
            "classifier returned JSON, but not an array",
            "classifier array contained a non-object item",
        ))
        for v in verdicts
    )


def run_classification(conn, client, *, dry_run: bool = False) -> ClassificationResult:
    messages = load_unprocessed(conn, _batch_size())
    if not messages:
        return ClassificationResult(0, 0, 0)
    model_result = _call_model_result(client, messages)
    raw_response = model_result.text
    verdicts = verdicts_for(conn, messages, raw_response)
    malformed_output = _malformed_output(verdicts)
    if dry_run:
        for v in verdicts:
            status = "commit" if v.ok else "review"
            print(f"{v.gmail_msg_id}: {status}" + (f" -- {v.reason}" if v.reason else ""))
        return ClassificationResult(
            len(messages),
            sum(1 for v in verdicts if v.ok),
            sum(1 for v in verdicts if not v.ok),
            model_result.stats,
            malformed_output,
        )
    committed, reviewed = apply_verdicts(conn, verdicts)
    return ClassificationResult(len(messages), committed, reviewed, model_result.stats, malformed_output)


def _process_batch_unlocked(conn, client, *, dry_run: bool = False) -> tuple[int, int, int]:
    result = run_classification(conn, client, dry_run=dry_run)
    return result.seen, result.committed, result.reviewed


# Bumped only if a field is renamed or repurposed (not for a pure
# addition -- additive fields are always safe for a consumer using .get()).
# A long-lived JSONL log outlives the code that wrote it; a future reader
# (a dashboard, a cost report run over a year of history) needs a way to
# know whether row N predates a meaning change, not just guess from which
# fields happen to be present.
CLASSIFY_LOG_SCHEMA_VERSION = 1

# The exact set of fields every non-"start" classify log event carries,
# explicitly, even when the value is None -- never just omitted. "start" is
# the one exemption: it's a lifecycle marker for a run that hasn't done
# anything yet, not a result, so attempt/latency/token/exit-status fields
# would only ever be placeholders there.
#
# This exists because the three exception branches in main() used to build
# their field dicts by hand and quietly diverged from the success path:
# they never logged input_tokens/output_tokens at all (present-but-null and
# absent-entirely look identical in a `jq` one-liner, but not to a strict
# schema -- a DataFrame or a `json_each`-based SQL view over this log would
# show those columns as inconsistently present depending on which branch
# produced the row). One helper, used by every call site, is what makes
# "sum output_tokens across the whole log" a query that doesn't need a
# per-event-type special case.
def _base_log_fields(
    model: str, *, exit_status: int, attempt_count: int = 0,
    status_code: int | None = None, error_type: str | None = None,
    latency_ms: int | None = None, model_latency_ms: int | None = None,
    input_tokens: int | None = None, output_tokens: int | None = None,
    batch_size: int | None = None, error: str | None = None,
) -> dict[str, Any]:
    return {
        "model": model,
        "exit_status": exit_status,
        "attempt_count": attempt_count,
        "status_code": status_code,
        "error_type": error_type,
        "latency_ms": latency_ms,
        "model_latency_ms": model_latency_ms,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "batch_size": batch_size,
        "error": error,
    }


def _log_fields_for_result(result: ClassificationResult, model: str, latency_ms: int) -> dict[str, Any]:
    stats = result.model_stats
    fields = _base_log_fields(
        model,
        exit_status=0,
        attempt_count=stats.attempt_count if stats else 0,
        status_code=stats.status_code if stats else None,
        error_type=stats.error_type if stats else None,
        latency_ms=latency_ms,
        model_latency_ms=stats.latency_ms if stats else None,
        input_tokens=stats.input_tokens if stats else None,
        output_tokens=stats.output_tokens if stats else None,
        # Same number as `seen` by construction (both come from the same
        # len(messages)) -- present under this name too so batch_size is
        # the one field name that means "message count" across every
        # classify event type, success or failure.
        batch_size=result.seen,
    )
    fields.update({
        "seen": result.seen,
        "committed": result.committed,
        "reviewed": result.reviewed,
    })
    return fields


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
    model = selected_model()
    started = time.monotonic()
    # batch_size/min_confidence here are the *configured* values, so a
    # "why did everything route to review" question can be answered from
    # this one line (a lowered CLASSIFIER_MIN_CONFIDENCE, say) without
    # guessing whether it's a config change or an Anthropic-side problem.
    _structured_log(
        "start",
        dry_run=args.dry_run,
        model=model,
        batch_size_setting=_batch_size(),
        min_confidence=_min_confidence(),
    )
    conn = None
    try:
        conn = jt.connect()
        result = run_classification(
            conn, build_classifier_client(), dry_run=args.dry_run)
    except ModelCallError as e:
        event = "final_model_exhaustion" if e.exhausted else "non_retryable_api_failure"
        _structured_log(
            event,
            dry_run=args.dry_run,
            **_base_log_fields(
                model,
                exit_status=1,
                attempt_count=e.stats.attempt_count,
                status_code=e.stats.status_code,
                error_type=e.stats.error_type,
                latency_ms=int((time.monotonic() - started) * 1000),
                model_latency_ms=e.stats.latency_ms,
                input_tokens=e.stats.input_tokens,
                output_tokens=e.stats.output_tokens,
                # How many raw_messages are stuck unprocessed behind this
                # failure -- turns "Anthropic down" into a scoped,
                # on-call-ready fact ("N messages queued since <ts>")
                # instead of a guess.
                batch_size=e.stats.batch_size,
                error=str(e.original),
            ),
        )
        raise e.original from e
    except BaseException as e:
        code = e.code if isinstance(e, SystemExit) else 1
        _structured_log(
            "failure",
            dry_run=args.dry_run,
            **_base_log_fields(
                model,
                exit_status=code if isinstance(code, int) else 1,
                status_code=_status_code(e) if isinstance(e, Exception) else None,
                error_type=type(e).__name__,
                latency_ms=int((time.monotonic() - started) * 1000),
                # No ModelCallStats reaches this branch (it only handles
                # non-API exceptions -- a DB error, an import error, a bug
                # downstream of a *successful* model call). model_latency_ms/
                # input_tokens/output_tokens/batch_size stay at
                # _base_log_fields' None default -- explicit in the payload,
                # not omitted, so a dashboard can tell "no model info
                # because this wasn't an API failure" apart from a field
                # that's simply missing.
                error=str(e),
            ),
        )
        raise
    finally:
        if conn is not None:
            conn.close()
    print(f"processed {result.seen}, committed {result.committed}, review {result.reviewed}")
    latency_ms = int((time.monotonic() - started) * 1000)
    fields = _log_fields_for_result(result, model, latency_ms)
    if result.model_stats is not None and result.model_stats.retryable_failures_recovered:
        _structured_log("retryable_failures_recovered", dry_run=args.dry_run, **fields)
    if result.seen == 0:
        outcome = "healthy_zero_message_run"
    elif result.malformed_output:
        outcome = "malformed_classifier_output"
    else:
        outcome = "successful_processed_run"
    _structured_log(outcome, dry_run=args.dry_run, **fields)
    _structured_log(
        "success",
        dry_run=args.dry_run,
        outcome=outcome,
        **fields,
    )


if __name__ == "__main__":
    main()
