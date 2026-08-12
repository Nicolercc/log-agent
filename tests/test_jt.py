"""
Tests for jt.

Every test that touches staleness passes an explicit `today`. Nothing here
calls date.today() implicitly -- a suite whose assertions change at midnight
is a suite you learn to ignore.
"""

import sqlite3
from datetime import date

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
