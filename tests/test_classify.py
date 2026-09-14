import json
import fcntl
import re
import sys

import pytest

import classify
import jt


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("JT_DB", str(tmp_path / "test.db"))
    c = jt.connect()
    yield c
    c.close()


def add_app(conn, company="Company A", role="Backend Engineer",
            applied_on="2026-08-01"):
    cur = conn.execute(
        "INSERT INTO applications (company, role, lane, applied_on) VALUES (?,?,?,?)",
        (company, role, "swe", applied_on),
    )
    conn.commit()
    return cur.lastrowid


def add_raw(conn, msg_id="m1", body="we received your application today"):
    conn.execute(
        """INSERT INTO raw_messages
           (gmail_msg_id, received_on, sender, subject, body)
           VALUES (?,?,?,?,?)""",
        (msg_id, "2026-08-02", "jobs@example.com", "Thanks", body),
    )
    conn.commit()


def _fake_msg(msg_id, body="hello"):
    # prompt_for() indexes received_on/sender/subject/body directly, so a
    # bare {"gmail_msg_id": ...} dict raises KeyError deep inside
    # _call_model_result -- easy to mistake for the thing under test.
    return {
        "gmail_msg_id": msg_id,
        "received_on": "2026-08-02",
        "sender": "jobs@example.com",
        "subject": "Thanks",
        "body": body,
    }


def proposal(msg_id="m1", company="Company A", role_hint="Backend Engineer",
             kind="confirmed", evidence="we received your application"):
    return {
        "gmail_msg_id": msg_id,
        "company": company,
        "role_hint": role_hint,
        "kind": kind,
        "occurred_on": "2026-08-02",
        "confidence": 0.91,
        "evidence": evidence,
    }


class ToolUseBlock:
    """Stands in for the tool_use content block Anthropic returns when
    tool_choice forces a specific tool. .input is already a parsed dict --
    the real SDK does the JSON decoding, this fake mirrors that."""
    type = "tool_use"
    name = classify.TOOL_NAME

    def __init__(self, tool_input):
        self.input = tool_input


def _tool_response(proposals, usage=None):
    class Response:
        pass

    r = Response()
    r.content = [ToolUseBlock({"proposals": proposals})]
    r.usage = usage
    return r


def _no_tool_call_response():
    # Simulates the tool not being called at all (e.g. truncated before the
    # tool_use block completed) -- content has no tool_use block.
    class Response:
        pass

    r = Response()
    r.content = []
    r.usage = None
    return r


class FakeClient:
    """Takes the same `raw` JSON-array-or-garbage string the pre-tool-use
    tests were written against, and translates it into the tool-use
    response shape the real API now returns: valid JSON array text becomes
    that array as the tool's `proposals` input; anything else (the
    "malformed" test case) becomes a response with no tool_use block at
    all, since under forced tool_choice a garbled *string* is no longer
    representable -- the failure mode shifts to "the tool wasn't called"."""
    def __init__(self, raw):
        self.raw = raw

    class Messages:
        def __init__(self, outer):
            self.outer = outer

        def create(self, **kwargs):
            try:
                data = json.loads(self.outer.raw)
            except json.JSONDecodeError:
                return _no_tool_call_response()
            if not isinstance(data, list):
                return _no_tool_call_response()
            return _tool_response(data)

    @property
    def messages(self):
        return self.Messages(self)


def test_passing_proposal_commits_event_and_marks_processed(conn):
    add_app(conn)
    add_raw(conn)
    raw = json.dumps([proposal()])

    seen, committed, reviewed = classify.process_batch(conn, FakeClient(raw))

    assert (seen, committed, reviewed) == (1, 1, 0)
    assert conn.execute("SELECT kind FROM events").fetchone()[0] == "confirmed"
    assert conn.execute("SELECT processed FROM raw_messages").fetchone()[0] == 1


def test_malformed_json_goes_to_review_and_marks_processed(conn):
    add_app(conn)
    add_raw(conn)

    seen, committed, reviewed = classify.process_batch(conn, FakeClient("not json"))

    assert (seen, committed, reviewed) == (1, 0, 1)
    assert "malformed JSON" in conn.execute("SELECT reason FROM review_queue").fetchone()[0]
    assert conn.execute("SELECT processed FROM raw_messages").fetchone()[0] == 1


def test_unknown_kind_goes_to_review(conn):
    add_app(conn)
    add_raw(conn)
    p = proposal(kind="promoted")

    classify.process_batch(conn, FakeClient(json.dumps([p])))

    assert "unknown event kind" in conn.execute("SELECT reason FROM review_queue").fetchone()[0]
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_hallucinated_evidence_goes_to_review(conn):
    add_app(conn)
    add_raw(conn, body="completely different body text")

    classify.process_batch(conn, FakeClient(json.dumps([proposal()])))

    assert "evidence is not" in conn.execute("SELECT reason FROM review_queue").fetchone()[0]


def test_unmatched_classifier_proposal_creates_application_candidate(conn):
    add_raw(conn)

    seen, committed, reviewed = classify.process_batch(
        conn, FakeClient(json.dumps([proposal()])))

    assert (seen, committed, reviewed) == (1, 0, 1)
    assert conn.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0] == 0
    row = conn.execute("SELECT * FROM application_candidates").fetchone()
    assert row["source"] == "classifier"
    assert row["source_ref"] == "gmail:m1"
    assert row["company"] == "Company A"
    assert row["role"] == "Backend Engineer"
    assert row["applied_on"] == "2026-08-02"
    assert row["status"] == "pending"
    assert conn.execute("SELECT processed FROM raw_messages").fetchone()[0] == 1


def test_repeated_unmatched_classifier_proposal_is_a_noop_candidate(conn):
    add_raw(conn)
    raw = json.dumps([proposal()])

    classify.process_batch(conn, FakeClient(raw))
    conn.execute("UPDATE raw_messages SET processed = 0 WHERE gmail_msg_id = 'm1'")
    conn.commit()
    classify.process_batch(conn, FakeClient(raw))

    assert conn.execute("SELECT COUNT(*) FROM application_candidates").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_same_company_two_roles_requires_role_hint(conn):
    add_app(conn, company="Company A", role="Backend Engineer")
    add_app(conn, company="Company A", role="Platform Engineer")
    add_raw(conn)
    p = proposal(role_hint="")

    classify.process_batch(conn, FakeClient(json.dumps([p])))

    assert "ambiguous role" in conn.execute("SELECT reason FROM review_queue").fetchone()[0]


def test_illegal_transition_goes_to_review(conn):
    app_id = add_app(conn)
    conn.execute(
        "INSERT INTO events (application_id, occurred_on, kind) VALUES (?,?,?)",
        (app_id, "2026-08-02", "rejected"),
    )
    add_raw(conn, msg_id="m2", body="we would like to schedule onsite interview")
    p = proposal(msg_id="m2", kind="onsite", evidence="would like to schedule onsite")

    classify.process_batch(conn, FakeClient(json.dumps([p])))

    assert "terminal" in conn.execute("SELECT reason FROM review_queue").fetchone()[0]


def test_one_message_with_multiple_proposals_goes_to_review(conn):
    add_app(conn)
    add_raw(conn)
    raw = json.dumps([proposal(), proposal(kind="screen")])

    classify.process_batch(conn, FakeClient(raw))

    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0] == 2


def test_event_before_application_date_goes_to_review(conn):
    add_app(conn, applied_on="2026-08-03")
    add_raw(conn)

    classify.process_batch(conn, FakeClient(json.dumps([proposal()])))

    assert "predates application date" in conn.execute(
        "SELECT reason FROM review_queue").fetchone()[0]


def test_duplicate_event_is_not_counted_as_commit(conn):
    add_app(conn)
    other_app_id = add_app(conn, company="OtherCo", role="Engineer")
    add_raw(conn)
    conn.execute(
        """INSERT INTO events
           (application_id, occurred_on, kind, gmail_msg_id)
           VALUES (?,?,?,?)""",
        (other_app_id, "2026-08-02", "confirmed", "m1"),
    )
    conn.commit()

    seen, committed, reviewed = classify.process_batch(
        conn, FakeClient(json.dumps([proposal()])))

    assert (seen, committed, reviewed) == (1, 0, 1)
    assert "duplicate gmail_msg_id" in conn.execute(
        "SELECT reason FROM review_queue").fetchone()[0]


def test_classifier_lock_refuses_second_process(conn, tmp_path):
    lock_path = tmp_path / "test.db.classify.lock"
    with lock_path.open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(SystemExit, match="another jt-classify"):
            with classify.classifier_lock(lock_path):
                pass
        fcntl.flock(f, fcntl.LOCK_UN)


def test_call_model_retries_retryable_errors():
    class Retryable(Exception):
        status_code = 429

    class Client:
        def __init__(self):
            self.calls = 0

        class Messages:
            def __init__(self, outer):
                self.outer = outer

            def create(self, **kwargs):
                self.outer.calls += 1
                if self.outer.calls == 1:
                    raise Retryable("slow down")
                return _tool_response([])

        @property
        def messages(self):
            return self.Messages(self)

    client = Client()

    assert classify.call_model(client, [], sleep=lambda _: None) == "[]"
    assert client.calls == 2


def test_call_model_uses_current_default_model(monkeypatch):
    monkeypatch.delenv("CLASSIFIER_MODEL", raising=False)

    class Client:
        def __init__(self):
            self.model = None

        class Messages:
            def __init__(self, outer):
                self.outer = outer

            def create(self, **kwargs):
                self.outer.model = kwargs["model"]
                return _tool_response([])

        @property
        def messages(self):
            return self.Messages(self)

    client = Client()

    assert classify.call_model(client, [], sleep=lambda _: None) == "[]"
    # Asserted against the module constant, not a duplicated literal -- a
    # literal here would keep passing even if DEFAULT_MODEL drifted to a
    # stale/invalid model string, which is exactly how that went unnoticed
    # before (twice now: this test's literal was itself reverted to the
    # stale value in a later edit and still "passed").
    assert client.model == classify.DEFAULT_MODEL


def test_default_model_is_a_real_current_sonnet_id():
    # Anthropic model IDs are "claude-<family>-<generation>" (an unversioned
    # alias, e.g. "claude-sonnet-5") or that plus a "-YYYYMMDD" dated
    # snapshot suffix -- never a "-<major>-<minor>" version pair. That shape
    # is what let a stale/invented model ID ("claude-sonnet-4-6") sit as the
    # default undetected: it looked plausible but resolved to nothing. This
    # exact value has now been reverted back to that stale string three
    # times in one session (each time a different piece of surrounding code
    # changed); the format assertion is what makes a fourth revert fail
    # instead of silently passing, so don't drop it if this test gets
    # rewritten again.
    assert classify.DEFAULT_MODEL == "claude-sonnet-5"
    assert re.fullmatch(r"claude-[a-z]+-\d+(-\d{8})?", classify.DEFAULT_MODEL)


# --------------------------------------------------------------------------
# Schema-typed API boundary (tool-use instead of free-text JSON)
# --------------------------------------------------------------------------

def test_tool_schema_excludes_note_and_otherwise_matches_event_kinds():
    # Prompt/schema alignment, concretely: the schema's enum is the actual
    # source of truth the API enforces, so it must be derived from
    # jt.EVENT_KINDS (not a separately hand-maintained list that can drift)
    # and must exclude "note" the same way validate_proposal() forbids it.
    schema = classify.classification_tool_schema()
    enum = schema["input_schema"]["properties"]["proposals"]["items"]["properties"]["kind"]["enum"]
    assert "note" not in enum
    assert set(enum) == set(jt.EVENT_KINDS) - {"note"}
    assert schema["name"] == classify.TOOL_NAME


def test_tool_schema_requires_every_field_validate_proposal_needs():
    # If the schema's `required` list and validate_proposal()'s own
    # _field_types() required set ever diverge, the schema stops narrowing
    # what validate_proposal has to check -- catch that here rather than
    # noticing it as a gap in production.
    schema = classify.classification_tool_schema()
    required = set(schema["input_schema"]["properties"]["proposals"]["items"]["required"])
    assert required == {
        "gmail_msg_id", "company", "role_hint", "kind",
        "occurred_on", "confidence", "evidence",
    }


def test_call_model_forces_the_tool_via_tool_choice():
    # The whole "schema-typed at the API boundary" claim rests on the call
    # actually forcing the tool, not just offering it as one option the
    # model could ignore in favor of a free-text reply. Assert the exact
    # kwargs reaching the API, not just the parsed result.
    captured = {}

    class Client:
        class Messages:
            def create(self, **kwargs):
                captured.update(kwargs)
                return _tool_response([])

        @property
        def messages(self):
            return self.Messages()

    classify._call_model_result(Client(), [_fake_msg("m1")])

    assert captured["tool_choice"] == {"type": "tool", "name": classify.TOOL_NAME}
    assert len(captured["tools"]) == 1
    assert captured["tools"][0]["name"] == classify.TOOL_NAME


def test_missing_tool_use_block_routes_batch_to_review_not_silent_success(conn):
    # Under forced tool_choice this should be unreachable in real use (the
    # one plausible cause is truncation before the tool call completes),
    # but if it ever happens, nothing may commit silently -- every message
    # in the batch must land in review_queue with an honest reason.
    add_app(conn)
    add_raw(conn)

    seen, committed, reviewed = classify.process_batch(conn, FakeClient("not json"))

    assert (seen, committed, reviewed) == (1, 0, 1)
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    reason = conn.execute("SELECT reason FROM review_queue").fetchone()[0]
    assert "malformed JSON" in reason


def test_validate_proposal_still_rejects_note_kind_as_defense_in_depth(conn):
    # The tool schema excludes "note" from the enum, so the real API path
    # can no longer produce one -- but validate_proposal() must still
    # reject it directly, both because constrained decoding is an
    # implementation detail Anthropic doesn't contractually guarantee, and
    # because evals/run.py's --use-expected path builds proposals from
    # fixture data and calls verdicts_for() directly, bypassing the tool
    # schema entirely.
    add_app(conn)
    messages_by_id = {"m1": {"gmail_msg_id": "m1", "body": "we received your application"}}
    p = proposal(kind="note", evidence="we received your application")

    verdict = classify.validate_proposal(conn, p, messages_by_id)

    assert verdict.ok is False
    assert "may not write human note events" in verdict.reason


def test_classify_structured_log_writes_jsonl(tmp_path, monkeypatch):
    log_path = tmp_path / "classify.jsonl"
    monkeypatch.setattr(classify, "LOG_PATH", log_path)

    classify._structured_log("success", exit_status=0, seen=1, committed=1, reviewed=0)

    row = json.loads(log_path.read_text(encoding="utf-8"))
    assert row["component"] == "jt-classify"
    assert row["event"] == "success"
    assert row["committed"] == 1
    assert row["reviewed"] == 0


def test_main_logs_classifier_model_override(tmp_path, monkeypatch):
    log_path = tmp_path / "classify.jsonl"
    monkeypatch.setattr(classify, "LOG_PATH", log_path)
    monkeypatch.setenv("CLASSIFIER_MODEL", "claude-override-test")
    monkeypatch.setattr(sys, "argv", ["classify.py", "--dry-run"])

    class Conn:
        def close(self):
            pass

    monkeypatch.setattr(classify.jt, "connect", lambda: Conn())
    monkeypatch.setattr(classify, "build_classifier_client", lambda: object())
    monkeypatch.setattr(
        classify,
        "run_classification",
        lambda conn, client, dry_run=False: classify.ClassificationResult(0, 0, 0),
    )

    classify.main()

    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["event"] == "start"
    assert rows[0]["model"] == "claude-override-test"
    # The outcome line carries the model too, so which model produced a
    # given commit/review batch is readable from that one line without
    # correlating back to the run's start line.
    assert rows[-1]["event"] == "success"
    assert rows[-1]["model"] == "claude-override-test"


def test_main_logs_healthy_zero_message_run(tmp_path, monkeypatch):
    log_path = tmp_path / "classify.jsonl"
    monkeypatch.setattr(classify, "LOG_PATH", log_path)
    monkeypatch.delenv("CLASSIFIER_MODEL", raising=False)
    monkeypatch.setattr(sys, "argv", ["classify.py", "--dry-run"])

    class Conn:
        def close(self):
            pass

    monkeypatch.setattr(classify.jt, "connect", lambda: Conn())
    monkeypatch.setattr(classify, "build_classifier_client", lambda: object())
    monkeypatch.setattr(
        classify,
        "run_classification",
        lambda conn, client, dry_run=False: classify.ClassificationResult(0, 0, 0),
    )

    classify.main()

    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    zero = next(row for row in rows if row["event"] == "healthy_zero_message_run")
    assert zero["model"] == classify.DEFAULT_MODEL
    assert zero["attempt_count"] == 0
    assert zero["status_code"] is None
    assert zero["error_type"] is None
    assert "latency_ms" in zero


def test_main_logs_successful_processed_run(tmp_path, monkeypatch):
    log_path = tmp_path / "classify.jsonl"
    monkeypatch.setattr(classify, "LOG_PATH", log_path)
    monkeypatch.setattr(sys, "argv", ["classify.py", "--dry-run"])

    class Conn:
        def close(self):
            pass

    stats = classify.ModelCallStats(
        attempt_count=1,
        status_code=None,
        error_type=None,
        latency_ms=12,
    )
    monkeypatch.setattr(classify.jt, "connect", lambda: Conn())
    monkeypatch.setattr(classify, "build_classifier_client", lambda: object())
    monkeypatch.setattr(
        classify,
        "run_classification",
        lambda conn, client, dry_run=False: classify.ClassificationResult(2, 1, 1, stats),
    )

    classify.main()

    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    processed = next(row for row in rows if row["event"] == "successful_processed_run")
    assert processed["attempt_count"] == 1
    assert processed["model_latency_ms"] == 12
    assert processed["seen"] == 2


def test_main_logs_retryable_failures_recovered(tmp_path, monkeypatch):
    log_path = tmp_path / "classify.jsonl"
    monkeypatch.setattr(classify, "LOG_PATH", log_path)
    monkeypatch.setattr(sys, "argv", ["classify.py", "--dry-run"])

    class Conn:
        def close(self):
            pass

    stats = classify.ModelCallStats(
        attempt_count=3,
        status_code=503,
        error_type="ServiceUnavailable",
        latency_ms=25,
        retryable_failures_recovered=True,
    )
    monkeypatch.setattr(classify.jt, "connect", lambda: Conn())
    monkeypatch.setattr(classify, "build_classifier_client", lambda: object())
    monkeypatch.setattr(
        classify,
        "run_classification",
        lambda conn, client, dry_run=False: classify.ClassificationResult(1, 1, 0, stats),
    )

    classify.main()

    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    recovered = next(row for row in rows if row["event"] == "retryable_failures_recovered")
    assert recovered["attempt_count"] == 3
    assert recovered["status_code"] == 503
    assert recovered["error_type"] == "ServiceUnavailable"


def test_main_logs_malformed_classifier_output(tmp_path, monkeypatch):
    log_path = tmp_path / "classify.jsonl"
    monkeypatch.setattr(classify, "LOG_PATH", log_path)
    monkeypatch.setattr(sys, "argv", ["classify.py", "--dry-run"])

    class Conn:
        def close(self):
            pass

    stats = classify.ModelCallStats(
        attempt_count=1,
        status_code=None,
        error_type=None,
        latency_ms=8,
    )
    monkeypatch.setattr(classify.jt, "connect", lambda: Conn())
    monkeypatch.setattr(classify, "build_classifier_client", lambda: object())
    monkeypatch.setattr(
        classify,
        "run_classification",
        lambda conn, client, dry_run=False: classify.ClassificationResult(1, 0, 1, stats, True),
    )

    classify.main()

    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    malformed = next(row for row in rows if row["event"] == "malformed_classifier_output")
    assert malformed["attempt_count"] == 1
    assert malformed["reviewed"] == 1


def test_call_model_raises_model_call_error_after_5xx_exhaustion(monkeypatch):
    monkeypatch.delenv("CLASSIFIER_MODEL", raising=False)

    class ServerError(Exception):
        status_code = 500

    class Client:
        def __init__(self):
            self.calls = 0

        class Messages:
            def __init__(self, outer):
                self.outer = outer

            def create(self, **kwargs):
                self.outer.calls += 1
                raise ServerError("server unavailable")

        @property
        def messages(self):
            return self.Messages(self)

    client = Client()

    with pytest.raises(classify.ModelCallError) as excinfo:
        classify._call_model_result(client, [], sleep=lambda _: None)

    assert client.calls == classify.MAX_MODEL_ATTEMPTS
    assert excinfo.value.exhausted is True
    assert excinfo.value.stats.attempt_count == classify.MAX_MODEL_ATTEMPTS
    assert excinfo.value.stats.status_code == 500
    assert excinfo.value.stats.error_type == "ServerError"


def test_main_logs_final_model_exhaustion(tmp_path, monkeypatch):
    log_path = tmp_path / "classify.jsonl"
    monkeypatch.setattr(classify, "LOG_PATH", log_path)
    monkeypatch.setattr(sys, "argv", ["classify.py", "--dry-run"])

    class Conn:
        def close(self):
            pass

    class ServerError(Exception):
        status_code = 500

    stats = classify.ModelCallStats(
        attempt_count=classify.MAX_MODEL_ATTEMPTS,
        status_code=500,
        error_type="ServerError",
        latency_ms=31,
    )

    monkeypatch.setattr(classify.jt, "connect", lambda: Conn())
    monkeypatch.setattr(classify, "build_classifier_client", lambda: object())
    monkeypatch.setattr(
        classify,
        "run_classification",
        lambda conn, client, dry_run=False: (_ for _ in ()).throw(
            classify.ModelCallError(ServerError("server unavailable"), stats, exhausted=True)
        ),
    )

    with pytest.raises(ServerError):
        classify.main()

    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    exhausted = next(row for row in rows if row["event"] == "final_model_exhaustion")
    assert exhausted["attempt_count"] == classify.MAX_MODEL_ATTEMPTS
    assert exhausted["status_code"] == 500
    assert exhausted["error_type"] == "ServerError"


def test_main_logs_non_retryable_api_failure(tmp_path, monkeypatch):
    log_path = tmp_path / "classify.jsonl"
    monkeypatch.setattr(classify, "LOG_PATH", log_path)
    monkeypatch.setattr(sys, "argv", ["classify.py", "--dry-run"])

    class Conn:
        def close(self):
            pass

    class BadRequest(Exception):
        status_code = 400

    stats = classify.ModelCallStats(
        attempt_count=1,
        status_code=400,
        error_type="BadRequest",
        latency_ms=7,
    )

    monkeypatch.setattr(classify.jt, "connect", lambda: Conn())
    monkeypatch.setattr(classify, "build_classifier_client", lambda: object())
    monkeypatch.setattr(
        classify,
        "run_classification",
        lambda conn, client, dry_run=False: (_ for _ in ()).throw(
            classify.ModelCallError(BadRequest("bad request"), stats, exhausted=False)
        ),
    )

    with pytest.raises(BadRequest):
        classify.main()

    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    failure = next(row for row in rows if row["event"] == "non_retryable_api_failure")
    assert failure["attempt_count"] == 1
    assert failure["status_code"] == 400
    assert failure["error_type"] == "BadRequest"


def test_call_model_result_captures_batch_size_and_token_usage():
    class Usage:
        input_tokens = 1234
        output_tokens = 56

    class Client:
        class Messages:
            def create(self, **kwargs):
                return _tool_response([], usage=Usage())

        @property
        def messages(self):
            return self.Messages()

    result = classify._call_model_result(Client(), [_fake_msg("m1"), _fake_msg("m2")])

    assert result.stats.batch_size == 2
    assert result.stats.input_tokens == 1234
    assert result.stats.output_tokens == 56


def test_call_model_result_tokens_absent_when_client_has_no_usage():
    class Client:
        class Messages:
            def create(self, **kwargs):
                r = _tool_response([])
                del r.usage  # deliberately no .usage attribute at all,
                             # like an older SDK response shape
                return r

        @property
        def messages(self):
            return self.Messages()

    result = classify._call_model_result(Client(), [])

    assert result.stats.input_tokens is None
    assert result.stats.output_tokens is None


def test_model_call_error_carries_batch_size_for_stuck_message_count():
    class ServerError(Exception):
        status_code = 500

    class Client:
        class Messages:
            def create(self, **kwargs):
                raise ServerError("down")

        @property
        def messages(self):
            return self.Messages()

    with pytest.raises(classify.ModelCallError) as excinfo:
        classify._call_model_result(
            Client(), [_fake_msg("m1"), _fake_msg("m2"), _fake_msg("m3")],
            sleep=lambda _: None,
        )

    assert excinfo.value.stats.batch_size == 3


def test_main_logs_configured_batch_size_and_min_confidence_on_start(tmp_path, monkeypatch):
    log_path = tmp_path / "classify.jsonl"
    monkeypatch.setattr(classify, "LOG_PATH", log_path)
    monkeypatch.setenv("CLASSIFIER_BATCH_SIZE", "7")
    monkeypatch.setenv("CLASSIFIER_MIN_CONFIDENCE", "0.9")
    monkeypatch.setattr(sys, "argv", ["classify.py", "--dry-run"])

    class Conn:
        def close(self):
            pass

    monkeypatch.setattr(classify.jt, "connect", lambda: Conn())
    monkeypatch.setattr(classify, "build_classifier_client", lambda: object())
    monkeypatch.setattr(
        classify,
        "run_classification",
        lambda conn, client, dry_run=False: classify.ClassificationResult(0, 0, 0),
    )

    classify.main()

    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    start = rows[0]
    assert start["event"] == "start"
    assert start["batch_size_setting"] == 7
    assert start["min_confidence"] == 0.9


def test_crashed_run_is_not_distinguishable_as_healthy_only_by_accident(tmp_path, monkeypatch):
    # The exit criterion in plain terms: a human (or an alert rule) reading
    # this log must never be able to mistake a crashed run for "nothing to
    # do." Prove it structurally, not just per-event: run the same main()
    # twice, once where the batch is genuinely empty and once where the
    # model call fails outright, and assert the two runs are distinguishable
    # on every axis an alert or a human would actually check.
    class Conn:
        def close(self):
            pass

    class ServerError(Exception):
        status_code = 500

    stats = classify.ModelCallStats(
        attempt_count=classify.MAX_MODEL_ATTEMPTS,
        status_code=500,
        error_type="ServerError",
        latency_ms=10,
        batch_size=4,
    )

    def run_healthy(tmp_path, monkeypatch):
        log_path = tmp_path / "healthy.jsonl"
        monkeypatch.setattr(classify, "LOG_PATH", log_path)
        monkeypatch.setattr(sys, "argv", ["classify.py", "--dry-run"])
        monkeypatch.setattr(classify.jt, "connect", lambda: Conn())
        monkeypatch.setattr(classify, "build_classifier_client", lambda: object())
        monkeypatch.setattr(
            classify, "run_classification",
            lambda conn, client, dry_run=False: classify.ClassificationResult(0, 0, 0),
        )
        classify.main()
        return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]

    def run_crashed(tmp_path, monkeypatch):
        log_path = tmp_path / "crashed.jsonl"
        monkeypatch.setattr(classify, "LOG_PATH", log_path)
        monkeypatch.setattr(sys, "argv", ["classify.py", "--dry-run"])
        monkeypatch.setattr(classify.jt, "connect", lambda: Conn())
        monkeypatch.setattr(classify, "build_classifier_client", lambda: object())
        monkeypatch.setattr(
            classify, "run_classification",
            lambda conn, client, dry_run=False: (_ for _ in ()).throw(
                classify.ModelCallError(ServerError("down"), stats, exhausted=True)
            ),
        )
        raised = False
        try:
            classify.main()
        except ServerError:
            raised = True
        return raised, [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]

    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    healthy_rows = run_healthy(tmp_path / "a", monkeypatch)
    crashed_raised, crashed_rows = run_crashed(tmp_path / "b", monkeypatch)

    # 1. The crash actually propagates -- automation (jt-automation.sh) sees
    #    a non-zero process exit, not a silent success.
    assert crashed_raised is True

    # 2. No line in the crashed run's log uses "success" or the healthy
    #    outcome name -- there is no event name a naive `grep success` or
    #    `grep healthy` alert could confuse with the crash.
    healthy_events = {row["event"] for row in healthy_rows}
    crashed_events = {row["event"] for row in crashed_rows}
    assert "success" in healthy_events
    assert "healthy_zero_message_run" in healthy_events
    assert crashed_events.isdisjoint({"success", "healthy_zero_message_run"})

    # 3. The crash's own line names how many messages are stuck, which the
    #    healthy run has no need to claim.
    exhaustion_row = next(r for r in crashed_rows if r["event"] == "final_model_exhaustion")
    assert exhaustion_row["batch_size"] == 4


# --------------------------------------------------------------------------
# Log schema: long-term analytics usefulness
# --------------------------------------------------------------------------

CANONICAL_CLASSIFY_LOG_FIELDS = {
    "model", "exit_status", "attempt_count", "status_code", "error_type",
    "latency_ms", "model_latency_ms", "input_tokens", "output_tokens",
    "batch_size", "error",
}


def test_every_non_start_classify_event_carries_the_full_canonical_field_set(tmp_path, monkeypatch):
    # The property this locks in: whatever produced a log row -- a clean
    # success, a zero-message run, malformed model output, retry
    # exhaustion, a non-retryable API error, a generic crash, or a
    # recovered retry -- the *set* of keys on that row is identical. A
    # future event type that forgets a field (as the three exception
    # branches originally did for input_tokens/output_tokens) fails this
    # test instead of silently shipping a log with inconsistent columns
    # that only gets noticed when someone finally tries to build a cost
    # report over months of history.
    log_path = tmp_path / "classify.jsonl"
    monkeypatch.setattr(classify, "LOG_PATH", log_path)
    monkeypatch.setattr(sys, "argv", ["classify.py", "--dry-run"])
    monkeypatch.delenv("CLASSIFIER_MODEL", raising=False)

    class Conn:
        def close(self):
            pass

    monkeypatch.setattr(classify.jt, "connect", lambda: Conn())
    monkeypatch.setattr(classify, "build_classifier_client", lambda: object())

    # 1. retryable_failures_recovered + successful_processed_run + success
    recovered_stats = classify.ModelCallStats(
        attempt_count=2, status_code=503, error_type="ServiceUnavailable",
        latency_ms=9, retryable_failures_recovered=True, batch_size=1,
        input_tokens=100, output_tokens=20,
    )
    monkeypatch.setattr(
        classify, "run_classification",
        lambda conn, client, dry_run=False: classify.ClassificationResult(1, 1, 0, recovered_stats),
    )
    classify.main()

    # 2. healthy_zero_message_run + success
    monkeypatch.setattr(
        classify, "run_classification",
        lambda conn, client, dry_run=False: classify.ClassificationResult(0, 0, 0),
    )
    classify.main()

    # 3. malformed_classifier_output + success
    malformed_stats = classify.ModelCallStats(
        attempt_count=1, status_code=None, error_type=None, latency_ms=5, batch_size=1,
    )
    monkeypatch.setattr(
        classify, "run_classification",
        lambda conn, client, dry_run=False: classify.ClassificationResult(1, 0, 1, malformed_stats, True),
    )
    classify.main()

    # 4. final_model_exhaustion
    class ServerError(Exception):
        status_code = 500

    exhaustion_stats = classify.ModelCallStats(
        attempt_count=5, status_code=500, error_type="ServerError", latency_ms=40, batch_size=3,
    )
    monkeypatch.setattr(
        classify, "run_classification",
        lambda conn, client, dry_run=False: (_ for _ in ()).throw(
            classify.ModelCallError(ServerError("down"), exhaustion_stats, exhausted=True)
        ),
    )
    with pytest.raises(ServerError):
        classify.main()

    # 5. non_retryable_api_failure
    class BadRequest(Exception):
        status_code = 400

    non_retryable_stats = classify.ModelCallStats(
        attempt_count=1, status_code=400, error_type="BadRequest", latency_ms=6, batch_size=2,
    )
    monkeypatch.setattr(
        classify, "run_classification",
        lambda conn, client, dry_run=False: (_ for _ in ()).throw(
            classify.ModelCallError(BadRequest("bad"), non_retryable_stats, exhausted=False)
        ),
    )
    with pytest.raises(BadRequest):
        classify.main()

    # 6. failure (a non-API exception)
    monkeypatch.setattr(
        classify, "run_classification",
        lambda conn, client, dry_run=False: (_ for _ in ()).throw(RuntimeError("db exploded")),
    )
    with pytest.raises(RuntimeError):
        classify.main()

    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    non_start_rows = [r for r in rows if r["event"] != "start"]
    seen_events = {r["event"] for r in non_start_rows}
    assert seen_events == {
        "retryable_failures_recovered", "successful_processed_run", "success",
        "healthy_zero_message_run", "malformed_classifier_output",
        "final_model_exhaustion", "non_retryable_api_failure", "failure",
    }

    for row in non_start_rows:
        missing = CANONICAL_CLASSIFY_LOG_FIELDS - set(row)
        assert not missing, f"{row['event']} row is missing fields: {missing}"

    # And the five questions this schema exists to answer, spot-checked
    # against real values, not just key presence.
    exhaustion = next(r for r in non_start_rows if r["event"] == "final_model_exhaustion")
    assert exhaustion["model"] == classify.DEFAULT_MODEL   # model used
    assert exhaustion["input_tokens"] is None               # tokens spent (none -- call never completed)
    assert exhaustion["output_tokens"] is None
    assert exhaustion["latency_ms"] is not None              # latency
    assert exhaustion["model_latency_ms"] == 40
    assert exhaustion["attempt_count"] == 5                  # attempts
    assert exhaustion["exit_status"] != 0                     # success/failure -> failure

    success_run = next(r for r in non_start_rows if r["event"] == "successful_processed_run")
    assert success_run["model"] == classify.DEFAULT_MODEL
    assert success_run["input_tokens"] == 100                # tokens spent
    assert success_run["output_tokens"] == 20
    assert success_run["latency_ms"] is not None              # latency
    assert success_run["attempt_count"] == 2                  # attempts
    assert success_run["exit_status"] == 0                     # success/failure -> success


def test_classify_log_rows_carry_a_schema_version(tmp_path, monkeypatch):
    log_path = tmp_path / "classify.jsonl"
    monkeypatch.setattr(classify, "LOG_PATH", log_path)

    classify._structured_log("success", exit_status=0)

    row = json.loads(log_path.read_text(encoding="utf-8"))
    assert row["schema_version"] == classify.CLASSIFY_LOG_SCHEMA_VERSION
    assert isinstance(row["schema_version"], int)
