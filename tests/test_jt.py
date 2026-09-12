"""
Tests for jt.

Every test that touches staleness passes an explicit `today`. Nothing here
calls date.today() implicitly -- a suite whose assertions change at midnight
is a suite you learn to ignore.
"""

import sqlite3
from datetime import date, datetime

import pytest

import jt
from transitions import is_legal


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("JT_DB", str(tmp_path / "test.db"))
    c = jt.connect()
    yield c
    c.close()


def add_app(conn, company="Acme", role="Engineer", lane="swe", applied_on="2026-08-03",
            contact=None):
    cur = conn.execute(
        "INSERT INTO applications (company, role, lane, applied_on, contact_email) "
        "VALUES (?,?,?,?,?)",
        (company, role, lane, applied_on, contact),
    )
    conn.commit()
    return conn.execute("SELECT * FROM applications WHERE id = ?",
                        (cur.lastrowid,)).fetchone()


def add_event(conn, app_id, kind, on, msg_id=None, confidence=None):
    conn.execute(
        "INSERT INTO events (application_id, occurred_on, kind, gmail_msg_id, confidence) "
        "VALUES (?,?,?,?,?)",
        (app_id, on, kind, msg_id, confidence),
    )
    conn.commit()


# --------------------------------------------------------------------------
# Invariant 1 -- append-only, enforced by the database
# --------------------------------------------------------------------------

def test_events_cannot_be_updated(conn):
    app = add_app(conn)
    add_event(conn, app["id"], "screen", "2026-08-05")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE events SET kind = 'offer' WHERE application_id = ?",
                     (app["id"],))


def test_events_cannot_be_deleted(conn):
    app = add_app(conn)
    add_event(conn, app["id"], "screen", "2026-08-05")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM events WHERE application_id = ?", (app["id"],))


def test_insert_or_replace_is_rejected_by_the_delete_trigger(conn):
    """Documents the trap: OR REPLACE is a DELETE + INSERT under the hood."""
    app = add_app(conn)
    add_event(conn, app["id"], "screen", "2026-08-05", msg_id="m1")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT OR REPLACE INTO events (application_id, occurred_on, kind, gmail_msg_id) "
            "VALUES (?,?,?,?)",
            (app["id"], "2026-08-06", "offer", "m1"),
        )


# --------------------------------------------------------------------------
# Invariant 3 -- database-owned idempotency
# --------------------------------------------------------------------------

def test_duplicate_gmail_msg_id_is_rejected(conn):
    app = add_app(conn)
    add_event(conn, app["id"], "confirmed", "2026-08-04", msg_id="msg_abc")
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        add_event(conn, app["id"], "confirmed", "2026-08-04", msg_id="msg_abc")


def test_insert_or_ignore_makes_resync_a_noop(conn):
    app = add_app(conn)
    for _ in range(3):
        conn.execute(
            "INSERT OR IGNORE INTO events (application_id, occurred_on, kind, gmail_msg_id) "
            "VALUES (?,?,?,?)",
            (app["id"], "2026-08-04", "confirmed", "msg_abc"),
        )
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert n == 1


def test_null_gmail_msg_ids_do_not_collide(conn):
    """Manual events all have NULL msg_id; SQLite allows many NULLs in UNIQUE."""
    app = add_app(conn)
    add_event(conn, app["id"], "note", "2026-08-04")
    add_event(conn, app["id"], "note", "2026-08-05")
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2


# --------------------------------------------------------------------------
# Invariant 4 -- kind is constrained at the database layer
# --------------------------------------------------------------------------

def test_unknown_event_kind_is_rejected(conn):
    app = add_app(conn)
    with pytest.raises(sqlite3.IntegrityError):
        add_event(conn, app["id"], "promoted", "2026-08-05")


def test_confidence_out_of_range_is_rejected(conn):
    app = add_app(conn)
    with pytest.raises(sqlite3.IntegrityError):
        add_event(conn, app["id"], "screen", "2026-08-05", msg_id="m9", confidence=1.4)


# --------------------------------------------------------------------------
# Invariant 5 -- applications with history cannot be deleted
# --------------------------------------------------------------------------

def test_application_with_events_cannot_be_deleted(conn):
    app = add_app(conn)
    add_event(conn, app["id"], "screen", "2026-08-05")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM applications WHERE id = ?", (app["id"],))


def test_application_without_events_can_be_deleted(conn):
    app = add_app(conn)
    conn.execute("DELETE FROM applications WHERE id = ?", (app["id"],))
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 0


def test_duplicate_application_is_rejected(conn):
    add_app(conn, "Acme", "Engineer", applied_on="2026-08-03")
    with pytest.raises(sqlite3.IntegrityError):
        add_app(conn, "Acme", "Engineer", applied_on="2026-08-03")


def test_add_rejects_empty_company_or_role(conn):
    parser = jt.build_parser()
    args = parser.parse_args(["add", "   ", "Engineer", "--lane", "swe"])
    with pytest.raises(SystemExit, match="company and role"):
        jt.cmd_add(conn, args)


# --------------------------------------------------------------------------
# derive()
# --------------------------------------------------------------------------

def test_status_defaults_to_applied(conn):
    app = add_app(conn)
    assert jt.derive(conn, app, today=date(2026, 8, 4))["status"] == "applied"


def test_highest_stage_wins_regardless_of_insertion_order(conn):
    app = add_app(conn)
    add_event(conn, app["id"], "onsite", "2026-08-10")
    add_event(conn, app["id"], "confirmed", "2026-08-04")
    assert jt.derive(conn, app, today=date(2026, 8, 11))["status"] == "onsite"


def test_terminal_beats_a_later_higher_stage(conn):
    """A rejection is not outranked by an offer that predates it."""
    app = add_app(conn)
    add_event(conn, app["id"], "offer", "2026-08-10")
    add_event(conn, app["id"], "rejected", "2026-08-12")
    d = jt.derive(conn, app, today=date(2026, 8, 13))
    assert d["status"] == "rejected"
    assert d["is_terminal"]


def test_most_recent_terminal_wins(conn):
    app = add_app(conn)
    add_event(conn, app["id"], "ghosted", "2026-08-06")
    add_event(conn, app["id"], "rejected", "2026-08-09")
    assert jt.derive(conn, app, today=date(2026, 8, 10))["status"] == "rejected"


def test_last_touch_tracks_the_latest_event(conn):
    app = add_app(conn, applied_on="2026-08-03")
    add_event(conn, app["id"], "followup_sent", "2026-08-11")
    d = jt.derive(conn, app, today=date(2026, 8, 12))
    assert d["last_touch"] == "2026-08-11"
    assert d["followups"] == 1


def test_had_response_ignores_robot_confirmations(conn):
    app = add_app(conn)
    add_event(conn, app["id"], "confirmed", "2026-08-04")
    assert not jt.derive(conn, app, today=date(2026, 8, 5))["had_response"]
    add_event(conn, app["id"], "screen", "2026-08-06")
    assert jt.derive(conn, app, today=date(2026, 8, 7))["had_response"]


# --------------------------------------------------------------------------
# business_days_since()
# --------------------------------------------------------------------------

@pytest.mark.parametrize("start,today,expected", [
    ("2026-08-12", date(2026, 8, 12), 0),    # same day
    ("2026-08-13", date(2026, 8, 12), 0),    # future date clamps to 0
    ("2026-08-10", date(2026, 8, 12), 2),    # Mon -> Wed
    ("2026-08-07", date(2026, 8, 10), 1),    # Fri -> Mon, weekend excluded
    ("2026-08-03", date(2026, 8, 17), 10),   # two full weeks
])
def test_business_days(start, today, expected):
    assert jt.business_days_since(start, today) == expected


# --------------------------------------------------------------------------
# action_for()
# --------------------------------------------------------------------------

def test_fresh_application_waits(conn):
    app = add_app(conn, applied_on="2026-08-11")
    assert jt.action_for(jt.derive(conn, app, today=date(2026, 8, 12))) == "wait"


def test_six_quiet_business_days_triggers_follow_up(conn):
    app = add_app(conn, applied_on="2026-08-03")
    assert jt.action_for(jt.derive(conn, app, today=date(2026, 8, 11))) == "FOLLOW UP"


def test_follow_up_resets_the_clock(conn):
    app = add_app(conn, applied_on="2026-08-03")
    add_event(conn, app["id"], "followup_sent", "2026-08-11")
    assert jt.action_for(jt.derive(conn, app, today=date(2026, 8, 12))) == "wait"


def test_one_nudge_only_then_close_out(conn):
    app = add_app(conn, applied_on="2026-07-06")
    add_event(conn, app["id"], "followup_sent", "2026-07-14")
    assert jt.action_for(jt.derive(conn, app, today=date(2026, 8, 12))) == "CLOSE OUT"


def test_terminal_never_asks_for_action(conn):
    app = add_app(conn, applied_on="2026-06-01")
    add_event(conn, app["id"], "rejected", "2026-06-10")
    assert jt.action_for(jt.derive(conn, app, today=date(2026, 8, 12))) == "-"


# --------------------------------------------------------------------------
# priority()
# --------------------------------------------------------------------------

def test_priority_score_rewards_contact_and_response(conn):
    cold = add_app(conn, "ColdCo", "Engineer", applied_on="2026-08-03")
    warm = add_app(conn, "WarmCo", "Engineer", applied_on="2026-08-03",
                   contact="recruiter@example.com")
    add_event(conn, warm["id"], "screen", "2026-08-05")

    cold_d = jt.derive(conn, cold, today=date(2026, 8, 12))
    warm_d = jt.derive(conn, warm, today=date(2026, 8, 12))

    assert jt.priority_score(warm, warm_d) > jt.priority_score(cold, cold_d)


def test_priority_terms_explain_closeout_penalty(conn):
    app = add_app(conn, applied_on="2026-07-06")
    add_event(conn, app["id"], "followup_sent", "2026-07-14")
    d = jt.derive(conn, app, today=date(2026, 8, 12))

    terms = dict(jt.priority_terms(app, d))

    assert terms["closeout"] == jt.WEIGHTS["closeout"]


# --------------------------------------------------------------------------
# transitions.py -- the Phase 3 gate
# --------------------------------------------------------------------------

@pytest.mark.parametrize("frm,to", [
    ("applied", "confirmed"),
    ("applied", "rejected"),
    ("screen", "onsite"),
    ("onsite", "offer"),
    ("offer", "accepted"),
    ("screen", "followup_sent"),
])
def test_legal_transitions(frm, to):
    ok, _ = is_legal(frm, to)
    assert ok


@pytest.mark.parametrize("frm,to", [
    ("rejected", "onsite"),      # the confidently-wrong-classifier case
    ("accepted", "screen"),
    ("applied", "offer"),        # no skipping the whole funnel
    ("withdrawn", "followup_sent"),
    ("bogus", "screen"),
])
def test_illegal_transitions(frm, to):
    ok, reason = is_legal(frm, to)
    assert not ok and reason


def test_every_event_kind_appears_in_the_transition_table():
    """Guards against adding a kind to jt.py and forgetting the gate."""
    from transitions import ALWAYS_LEGAL_IF_OPEN, TRANSITIONS
    reachable = set().union(*TRANSITIONS.values()) | ALWAYS_LEGAL_IF_OPEN | {"note"}
    assert set(jt.EVENT_KINDS) <= reachable


# --------------------------------------------------------------------------
# Migration
# --------------------------------------------------------------------------

def test_v1_database_migrates_without_losing_events(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(path)
    legacy.executescript("""
        CREATE TABLE applications (
            id INTEGER PRIMARY KEY, company TEXT NOT NULL, role TEXT NOT NULL,
            lane TEXT NOT NULL, applied_on DATE NOT NULL, source TEXT,
            contact_email TEXT, url TEXT, notes TEXT,
            UNIQUE (company, role, applied_on));
        CREATE TABLE events (
            id INTEGER PRIMARY KEY,
            application_id INTEGER NOT NULL
                REFERENCES applications(id) ON DELETE CASCADE,
            occurred_on DATE NOT NULL, kind TEXT NOT NULL,
            gmail_msg_id TEXT UNIQUE, confidence REAL, evidence TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')));
        INSERT INTO applications (company, role, lane, applied_on)
            VALUES ('Acme','Engineer','swe','2026-08-03');
        INSERT INTO events (application_id, occurred_on, kind)
            VALUES (1,'2026-08-05','screen');
    """)
    legacy.commit()
    legacy.close()

    monkeypatch.setenv("JT_DB", str(path))
    c = jt.connect()
    try:
        assert c.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
        assert c.execute("SELECT kind FROM events").fetchone()[0] == "screen"
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            c.execute("DELETE FROM events")
    finally:
        c.close()


# --------------------------------------------------------------------------
# review resolution
# --------------------------------------------------------------------------

def test_resolve_requires_existing_queue_item(conn):
    parser = jt.build_parser()
    args = parser.parse_args(["resolve", "99"])
    with pytest.raises(SystemExit, match="no review queue item"):
        jt.cmd_resolve(conn, args)


def test_resolve_can_link_to_event(conn):
    app = add_app(conn)
    add_event(conn, app["id"], "screen", "2026-08-05")
    event_id = conn.execute("SELECT id FROM events").fetchone()[0]
    conn.execute(
        """INSERT INTO review_queue (gmail_msg_id, proposed_json, reason)
           VALUES ('m1', '{}', 'needs human')"""
    )
    conn.commit()

    parser = jt.build_parser()
    args = parser.parse_args(["resolve", "1", "--event-id", str(event_id), "--note", "handled"])
    jt.cmd_resolve(conn, args)

    row = conn.execute("SELECT * FROM review_queue WHERE id = 1").fetchone()
    assert row["resolved"] == 1
    assert row["resolved_event_id"] == event_id
    assert row["resolved_note"] == "handled"


def test_review_stale_alerts_on_queue_size_and_oldest_age(conn, tmp_path, capsys):
    conn.execute(
        """INSERT INTO review_queue (gmail_msg_id, proposed_json, reason, created_at)
           VALUES ('m1', '{}', 'needs human', '2026-08-01 00:00:00')"""
    )
    conn.execute(
        """INSERT INTO review_queue (gmail_msg_id, proposed_json, reason, created_at)
           VALUES ('m2', '{}', 'needs human', '2026-08-03 00:00:00')"""
    )
    conn.commit()

    alerts, stats = jt.review_alerts(
        conn,
        log_path=tmp_path / "missing.jsonl",
        now=datetime(2026, 8, 5, 12, 0, 0),
        queue_threshold=2,
        age_days=3,
        failure_threshold=3,
    )

    assert stats["review"]["total"] == 2
    assert stats["review"]["oldest_age_days"] == 4
    assert any("total_review_queue_size=2" in alert for alert in alerts)
    assert any("oldest_review_item_age_days=4" in alert for alert in alerts)

    parser = jt.build_parser()
    args = parser.parse_args([
        "review", "stale", "--queue-threshold", "2", "--age-days", "3",
        "--log", str(tmp_path / "missing.jsonl"),
    ])
    with pytest.raises(SystemExit) as excinfo:
        jt.cmd_review(conn, args)
    assert excinfo.value.code == 1
    out = capsys.readouterr().out
    assert "ALERT total_review_queue_size=2" in out
    assert "ALERT oldest_review_item_age_days=4" in out


def test_review_stale_alerts_on_classifier_exhaustion(conn, tmp_path):
    log_path = tmp_path / "jt-classify.jsonl"
    log_path.write_text(
        '\n'.join([
            '{"component":"jt-classify","event":"successful_processed_run"}',
            '{"component":"jt-classify","event":"final_model_exhaustion",'
            '"attempt_count":5,"status_code":500,"error_type":"ServerError"}',
        ]),
        encoding="utf-8",
    )

    alerts, stats = jt.review_alerts(
        conn, log_path=log_path, queue_threshold=99, age_days=99, failure_threshold=3)

    assert stats["classify"]["has_classifier_exhaustion"] is True
    assert any("classifier_exhaustion_event" in alert for alert in alerts)
    assert any("status_code=500" in alert for alert in alerts)


def test_review_stale_alerts_on_repeated_classify_failures(conn, tmp_path):
    log_path = tmp_path / "jt-classify.jsonl"
    log_path.write_text(
        '\n'.join([
            '{"component":"jt-classify","event":"successful_processed_run"}',
            '{"component":"jt-classify","event":"non_retryable_api_failure",'
            '"attempt_count":1,"status_code":400,"error_type":"BadRequest"}',
            '{"component":"jt-classify","event":"failure",'
            '"attempt_count":0,"status_code":null,"error_type":"RuntimeError"}',
        ]),
        encoding="utf-8",
    )

    alerts, stats = jt.review_alerts(
        conn, log_path=log_path, queue_threshold=99, age_days=99, failure_threshold=2)

    assert stats["classify"]["consecutive_failures"] == 2
    assert any("repeated_classify_failures=2" in alert for alert in alerts)


# --------------------------------------------------------------------------
# derive() -- terminal tie-break must use insertion order, not occurred_on
# --------------------------------------------------------------------------

def test_backdated_correction_overrides_an_earlier_dated_terminal_event(conn):
    """A correction is appended after the fact and is often dated to when it
    actually happened -- which can be earlier than the mistake it corrects.
    The most recently APPENDED terminal event must win, not the one with the
    latest occurred_on, or the correction is silently invisible."""
    app = add_app(conn)
    add_event(conn, app["id"], "rejected", "2026-08-10")
    add_event(conn, app["id"], "withdrawn", "2026-08-09")  # entered second, dated earlier
    d = jt.derive(conn, app, today=date(2026, 8, 15))
    assert d["status"] == "withdrawn"


# --------------------------------------------------------------------------
# cmd_log -- transitions.py must gate manual entry too, not just classify.py
# --------------------------------------------------------------------------

def test_log_refuses_an_illegal_transition(conn):
    app = add_app(conn)
    add_event(conn, app["id"], "rejected", "2026-08-05")
    parser = jt.build_parser()
    args = parser.parse_args(["log", str(app["id"]), "onsite"])
    with pytest.raises(SystemExit, match="terminal"):
        jt.cmd_log(conn, args)
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


def test_log_force_overrides_an_illegal_transition(conn):
    app = add_app(conn)
    add_event(conn, app["id"], "rejected", "2026-08-05")
    parser = jt.build_parser()
    args = parser.parse_args(["log", str(app["id"]), "onsite", "--force"])
    jt.cmd_log(conn, args)
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2


def test_log_note_is_exempt_from_the_transition_gate(conn):
    """note is deliberately excluded from transitions.py (see its docstring)
    because it's a human act with no bearing on status -- it must stay legal
    even on a terminal application, without needing --force."""
    app = add_app(conn)
    add_event(conn, app["id"], "rejected", "2026-08-05")
    parser = jt.build_parser()
    args = parser.parse_args(["log", str(app["id"]), "note", "--note", "closed the loop"])
    jt.cmd_log(conn, args)
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2


def test_log_allows_a_legal_transition_without_force(conn):
    app = add_app(conn)
    parser = jt.build_parser()
    args = parser.parse_args(["log", str(app["id"]), "screen"])
    jt.cmd_log(conn, args)
    assert jt.derive(conn, app)["status"] == "screen"


# --------------------------------------------------------------------------
# Migration -- must be atomic and must not silently drop invalid legacy rows
# --------------------------------------------------------------------------

def _make_legacy_db(path, extra_sql=""):
    legacy = sqlite3.connect(path)
    legacy.executescript(f"""
        CREATE TABLE applications (
            id INTEGER PRIMARY KEY, company TEXT NOT NULL, role TEXT NOT NULL,
            lane TEXT NOT NULL, applied_on DATE NOT NULL, source TEXT,
            contact_email TEXT, url TEXT, notes TEXT,
            UNIQUE (company, role, applied_on));
        CREATE TABLE events (
            id INTEGER PRIMARY KEY,
            application_id INTEGER NOT NULL
                REFERENCES applications(id) ON DELETE CASCADE,
            occurred_on DATE NOT NULL, kind TEXT NOT NULL,
            gmail_msg_id TEXT UNIQUE, confidence REAL, evidence TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')));
        INSERT INTO applications (company, role, lane, applied_on)
            VALUES ('Acme','Engineer','swe','2026-08-03');
        {extra_sql}
    """)
    legacy.commit()
    legacy.close()


def test_migration_refuses_a_legacy_kind_unknown_to_event_kinds(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    _make_legacy_db(path, """
        INSERT INTO events (application_id, occurred_on, kind)
            VALUES (1,'2026-08-05','phone_screen');
    """)
    monkeypatch.setenv("JT_DB", str(path))
    with pytest.raises(RuntimeError, match="phone_screen"):
        jt.connect()
    # The refusal must roll the whole attempt back -- database untouched,
    # not left half-migrated with the data stranded in events_legacy.
    raw = sqlite3.connect(path)
    tables = {r[0] for r in raw.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "events_legacy" not in tables
    assert raw.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    assert raw.execute("SELECT kind FROM events").fetchone()[0] == "phone_screen"
    raw.close()


def test_migration_is_atomic_across_a_simulated_mid_run_crash(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    _make_legacy_db(path, """
        INSERT INTO events (application_id, occurred_on, kind)
            VALUES (1,'2026-08-05','screen');
        INSERT INTO events (application_id, occurred_on, kind)
            VALUES (1,'2026-08-06','onsite');
    """)
    monkeypatch.setenv("JT_DB", str(path))

    leaked = {}

    def crashing_migrate(conn):
        leaked["conn"] = conn
        conn.commit()
        old = conn.isolation_level
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ALTER TABLE events RENAME TO events_legacy")
        conn.execute(jt.EVENTS_DDL)
        conn.isolation_level = old
        raise RuntimeError("simulated crash mid-migration")

    with monkeypatch.context() as m:
        m.setattr(jt, "_migrate_v1_to_v2", crashing_migrate)
        with pytest.raises(RuntimeError, match="simulated crash"):
            jt.connect()
    # A real crash means the OS reclaims the file lock on process exit; here
    # we close the connection to release it the same way, without ever
    # calling commit() -- the pending transaction rolls back on close, same
    # as it would on an unclean process death.
    leaked["conn"].close()

    raw = sqlite3.connect(path)
    tables = {r[0] for r in raw.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert tables == {"applications", "events", "event_kinds"}
    assert raw.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2
    raw.close()

    # Real _migrate_v1_to_v2 is back in effect here -- retry should recover cleanly.
    conn = jt.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2
        kinds = {r[0] for r in conn.execute("SELECT kind FROM events").fetchall()}
        assert kinds == {"screen", "onsite"}
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM events")
    finally:
        conn.close()
