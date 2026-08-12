import json
import fcntl

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


class FakeClient:
    def __init__(self, raw):
        self.raw = raw

    class Messages:
        def __init__(self, outer):
            self.outer = outer

        def create(self, **kwargs):
            class Response:
                pass

            class Content:
                pass

            r = Response()
            c = Content()
            c.text = self.outer.raw
            r.content = [c]
            return r

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

                class Response:
                    pass

                class Content:
                    pass

                r = Response()
                c = Content()
                c.text = "[]"
                r.content = [c]
                return r

        @property
        def messages(self):
            return self.Messages(self)

    client = Client()

    assert classify.call_model(client, [], sleep=lambda _: None) == "[]"
    assert client.calls == 2
