#!/usr/bin/env python3
"""
jt - job application tracker

Single file. Standard library only. SQLite backed. Local first.

INVARIANTS. These are enforced by the database, not by convention, and an
agent working in this repo must not remove them:

  1. `events` is APPEND-ONLY. SQLite triggers abort any UPDATE or DELETE.
     Correcting a mistake means appending a corrective event. The mistake
     stays in history, which is the point.

  2. `applications` has NO status column. Status is DERIVED from the event
     log on every read. There is no status field for a classifier to write
     to, so a bad classification can only ever propose -- never commit.

  3. `events.gmail_msg_id` is UNIQUE. The DATABASE owns idempotency, not
     the Python, and definitely not the model. This is what lets Gmail sync
     be deliberately sloppy about its cursor: overlap the window, let the
     constraint eat the duplicates.

  4. `events.kind` is a foreign key into `event_kinds`. A CHECK constraint
     gives the same guarantee but cannot be altered in SQLite without a
     table rebuild -- painful once append-only triggers exist.

  5. `events.application_id` is ON DELETE RESTRICT. An application can be
     deleted only while it has zero events (see `jt rm`). Past the first
     event it is history, and history does not get erased.

TRAP FOR AGENTS AND FUTURE ME: `INSERT OR REPLACE` is internally a DELETE plus
an INSERT. SQLite only fires the delete trigger for it when
`PRAGMA recursive_triggers = ON`, which is OFF by default -- so on a default
connection, OR REPLACE punches a silent hole straight through invariant 1.
`connect()` turns the pragma on. Any code that opens this database WITHOUT
going through `connect()` reopens that hole. Always use `INSERT OR IGNORE`
on `events`, and always open the database through `connect()`.

Usage:
    jt add "Stripe" "Backend Engineer" --lane swe
    jt bulk < today.txt
    jt list
    jt stale
    jt log 3 screen --on 2026-08-19
    jt show 3
    jt export --lane swe --out pursuit.csv
    jt backup ~/Dropbox/jobtrack
"""

import argparse
import csv
import hashlib
import json
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from transitions import is_legal

SCHEMA_VERSION = 4

# --------------------------------------------------------------------------
# Domain vocabulary. You own this. Changing it is a schema-level decision.
# --------------------------------------------------------------------------

LANES = ("swe", "ops", "comms")

# Stage events advance the pipeline. Higher rank = further along.
STAGE_RANK = {
    "applied": 0,
    "confirmed": 1,   # ATS acknowledged receipt -- a robot, not a human
    "response": 2,    # a human replied
    "screen": 3,      # recruiter screen
    "tech": 4,        # technical / take-home
    "onsite": 5,
    "final": 6,
    "offer": 7,
}

# Terminal events end the story. The most recent one wins outright.
TERMINAL = ("rejected", "withdrawn", "accepted", "ghosted")

# Touch events reset the staleness clock but do not change stage.
TOUCH = ("followup_sent", "thankyou_sent", "note")

# Evidence a human on the other side engaged. Feeds Phase 4 scoring.
INBOUND = ("response", "screen", "tech", "onsite", "final", "offer",
           "rejected", "accepted")

EVENT_KINDS = tuple(k for k in STAGE_RANK if k != "applied") + TERMINAL + TOUCH

# Follow-up policy. One nudge, then let it go.
FOLLOWUP_AFTER_BUSINESS_DAYS = 6
CLOSEOUT_AFTER_BUSINESS_DAYS = 15

# Scoring weights for `jt priority`. Tune these without reading the scorer.
WEIGHTS = {
    # Software roles get the stronger default push.
    "swe_base": 2.0,
    # Ops/comms roles still matter, just with a lower base.
    "other_base": 1.0,
    # A real contact is a warm channel and deserves action.
    "contact": 3.0,
    # Human inbound engagement is stronger than an ATS confirmation.
    "had_response": 2.0,
    # Later pipeline stages deserve more attention.
    "stage_progress": 1.5,
    # Every quiet business day decays urgency.
    "quiet_day": -0.25,
    # After one nudge and fifteen quiet business days, close it out.
    "closeout": -10.0,
}

DEFAULT_REVIEW_QUEUE_ALERT_THRESHOLD = 1
DEFAULT_REVIEW_AGE_ALERT_DAYS = 2
DEFAULT_CLASSIFY_FAILURE_ALERT_THRESHOLD = 3
CLASSIFY_FAILURE_EVENTS = {"final_model_exhaustion", "non_retryable_api_failure", "failure"}
CLASSIFY_RECOVERY_EVENTS = {
    "healthy_zero_message_run",
    "successful_processed_run",
    "malformed_classifier_output",
    "success",
}


KINDS_DDL = """
CREATE TABLE IF NOT EXISTS event_kinds (
    kind     TEXT PRIMARY KEY,
    category TEXT NOT NULL   -- stage | terminal | touch
);
"""

EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS events (
    id             INTEGER PRIMARY KEY,
    application_id INTEGER NOT NULL
                   REFERENCES applications(id) ON DELETE RESTRICT,
    occurred_on    DATE NOT NULL,
    kind           TEXT NOT NULL REFERENCES event_kinds(kind),
    gmail_msg_id   TEXT UNIQUE,
    confidence     REAL CHECK (confidence IS NULL
                               OR (confidence >= 0.0 AND confidence <= 1.0)),
    evidence       TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    id            INTEGER PRIMARY KEY,
    company       TEXT NOT NULL,
    role          TEXT NOT NULL,
    lane          TEXT NOT NULL CHECK (lane IN ('swe','ops','comms')),
    applied_on    DATE NOT NULL,
    source        TEXT,
    contact_email TEXT,
    url           TEXT,
    notes         TEXT,
    UNIQUE (company, role, applied_on)
);
""" + EVENTS_DDL + """
CREATE INDEX IF NOT EXISTS idx_events_app ON events(application_id);

-- Invariant 1, enforced by the engine rather than by good intentions.
CREATE TRIGGER IF NOT EXISTS events_append_only_update
BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events is append-only: UPDATE forbidden. Append a corrective event instead.');
END;

CREATE TRIGGER IF NOT EXISTS events_append_only_delete
BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'events is append-only: DELETE forbidden. Append a corrective event instead.');
END;

-- Phase 2 writes here: raw messages land before anything interprets them.
CREATE TABLE IF NOT EXISTS raw_messages (
    gmail_msg_id TEXT PRIMARY KEY,
    received_on  DATE,
    sender       TEXT,
    subject      TEXT,
    body         TEXT,
    processed    INTEGER NOT NULL DEFAULT 0
);

-- Phase 3 writes here: anything the classifier is not sure about.
-- A GROWING QUEUE IS THE FAILURE SIGNAL. `jt stale` surfaces the count.
CREATE TABLE IF NOT EXISTS review_queue (
    id            INTEGER PRIMARY KEY,
    gmail_msg_id  TEXT,
    proposed_json TEXT NOT NULL,
    reason        TEXT NOT NULL,
    resolved      INTEGER NOT NULL DEFAULT 0,
    resolved_event_id INTEGER REFERENCES events(id),
    resolved_note TEXT,
    resolved_at TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Local intake for possible applications. Nothing here is authoritative:
-- a human must explicitly accept a pending candidate before applications
-- changes, and acceptance uses the same validated insert path as `jt add`.
CREATE TABLE IF NOT EXISTS application_candidates (
    id            INTEGER PRIMARY KEY,
    raw_input     TEXT NOT NULL,
    source        TEXT NOT NULL DEFAULT 'manual_capture',
    source_ref    TEXT UNIQUE,
    company       TEXT,
    role          TEXT,
    lane          TEXT CHECK (lane IS NULL OR lane IN ('swe','ops','comms')),
    applied_on    DATE,
    contact_email TEXT,
    url           TEXT,
    notes         TEXT,
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','accepted','rejected')),
    accepted_application_id INTEGER REFERENCES applications(id) ON DELETE RESTRICT,
    rejected_note TEXT,
    decided_at    TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_application_candidates_url_unique
ON application_candidates(url)
WHERE url IS NOT NULL;

CREATE TABLE IF NOT EXISTS sync_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


# --------------------------------------------------------------------------
# Plumbing
# --------------------------------------------------------------------------

def db_path() -> Path:
    p = os.environ.get("JT_DB")
    return Path(p).expanduser() if p else Path.home() / ".jobtrack" / "jobs.db"


def _seed_kinds(conn) -> None:
    rows = ([(k, "stage") for k in STAGE_RANK if k != "applied"]
            + [(k, "terminal") for k in TERMINAL]
            + [(k, "touch") for k in TOUCH])
    conn.executemany(
        "INSERT OR IGNORE INTO event_kinds (kind, category) VALUES (?,?)", rows)


def _is_legacy(conn) -> bool:
    """v1 shipped events with ON DELETE CASCADE and no kind FK.

    A leftover `events_legacy` table means an earlier migration attempt (on
    an older build) was interrupted after the rename but before it finished
    -- treat that as unmigrated too, rather than trusting whatever partial
    `events` table it left behind.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='events'"
    ).fetchone()
    legacy_shape = bool(row and "ON DELETE CASCADE" in (row[0] or ""))
    orphaned_rename = bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='events_legacy'"
    ).fetchone())
    return legacy_shape or orphaned_rename


def _migrate_v1_to_v2(conn) -> None:
    """Rebuild events with RESTRICT + kind FK, preserving every row.

    Runs as one real SQLite transaction. ALTER/CREATE/DROP TABLE are all
    transactional DDL in SQLite, so as long as executescript() (which forces
    an implicit commit before it runs) is never used here, a crash anywhere
    in this function leaves the database exactly as it was before migration
    started -- not half-migrated with history sitting in an orphaned table
    nobody looks at again.

    A `PRAGMA foreign_key_check` gate before the commit refuses to finish if
    any legacy row's `kind` isn't in `event_kinds`: silently letting such a
    row fall outside invariant 4 forever (invisible to every derive() call,
    still on disk) is worse than refusing to migrate.
    """
    conn.commit()  # close out the implicit transaction _seed_kinds left open
    recovering = bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='events_legacy'"
    ).fetchone())

    old_isolation = conn.isolation_level
    conn.isolation_level = None  # manual BEGIN/COMMIT/ROLLBACK below
    conn.execute("BEGIN IMMEDIATE")
    try:
        if recovering:
            # An older build's migration died after the rename but before
            # the copy/drop. Discard whatever partial 'events' table it left
            # behind and recover from the real data in events_legacy.
            conn.execute("DROP TABLE IF EXISTS events")
        else:
            conn.execute("ALTER TABLE events RENAME TO events_legacy")

        conn.execute(EVENTS_DDL)
        conn.execute("""
            INSERT INTO events
                (id, application_id, occurred_on, kind, gmail_msg_id,
                 confidence, evidence, created_at)
            SELECT id, application_id, occurred_on, kind, gmail_msg_id,
                   confidence, evidence, created_at
            FROM events_legacy
        """)

        violations = conn.execute("PRAGMA foreign_key_check(events)").fetchall()
        if violations:
            bad_kinds = sorted({
                conn.execute(
                    "SELECT kind FROM events WHERE rowid = ?", (v[1],)
                ).fetchone()[0]
                for v in violations
            })
            raise RuntimeError(
                "migration refused: legacy events has kind(s) not in "
                f"event_kinds: {', '.join(bad_kinds)}. This entire attempt "
                "rolls back -- your database is untouched, still on the old "
                "schema. Fix or remap those rows by hand, or add the "
                "kind(s) to jt.py's EVENT_KINDS, then re-run."
            )

        conn.execute("DROP TABLE events_legacy")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.isolation_level = old_isolation
    print("migrated database to schema v2 (append-only enforced)", file=sys.stderr)


def _ensure_review_columns(conn) -> None:
    cols = {
        row["name"] if isinstance(row, sqlite3.Row) else row[1]
        for row in conn.execute("PRAGMA table_info(review_queue)")
    }
    additions = {
        "resolved_event_id": "ALTER TABLE review_queue ADD COLUMN resolved_event_id INTEGER REFERENCES events(id)",
        "resolved_note": "ALTER TABLE review_queue ADD COLUMN resolved_note TEXT",
        "resolved_at": "ALTER TABLE review_queue ADD COLUMN resolved_at TEXT",
    }
    for name, ddl in additions.items():
        if name not in cols:
            conn.execute(ddl)


def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row

    # Without this, `INSERT OR REPLACE` performs its implicit DELETE WITHOUT
    # firing the delete trigger -- a silent hole straight through invariant 1.
    # It is a per-connection pragma, so any other client (a psql-style shell,
    # an agent using sqlite3 directly) reopens the hole. Hence the belt-and-
    # braces: pragma here, and a loud warning in the module docstring.
    conn.execute("PRAGMA recursive_triggers = ON")

    # FKs off during setup so migration can rebuild tables safely.
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.executescript(KINDS_DDL)
    _seed_kinds(conn)
    if _is_legacy(conn):
        _migrate_v1_to_v2(conn)
    conn.executescript(SCHEMA)
    _ensure_review_columns(conn)
    conn.execute(
        "INSERT OR REPLACE INTO sync_state (key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def parse_day(s: str | None, today: date | None = None) -> str:
    today = today or date.today()
    if not s or s == "today":
        return today.isoformat()
    if s == "yesterday":
        return (today - timedelta(days=1)).isoformat()
    try:
        return date.fromisoformat(s).isoformat()
    except ValueError:
        sys.exit(f"error: date must be YYYY-MM-DD, 'today' or 'yesterday' (got {s!r})")


def business_days_since(iso_day: str, today: date | None = None) -> int:
    """Injectable clock. Tests cannot control date.today(), and staleness
    assertions that silently rot overnight produce CI you learn to ignore."""
    today = today or date.today()
    d = date.fromisoformat(iso_day)
    if d >= today:
        return 0
    n, cur = 0, d
    while cur < today:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            n += 1
    return n


# --------------------------------------------------------------------------
# Derived state -- the heart of the design
# --------------------------------------------------------------------------

def derive(conn, app_row, today: date | None = None) -> dict:
    """Compute current status from the event log. Never stored, always computed."""
    events = conn.execute(
        "SELECT * FROM events WHERE application_id = ? ORDER BY occurred_on, id",
        (app_row["id"],),
    ).fetchall()

    terminal = [e for e in events if e["kind"] in TERMINAL]
    if terminal:
        # The most recently APPENDED terminal event wins, not the one with
        # the latest occurred_on. A correction is entered after the fact and
        # is often dated to when it actually happened, which can be earlier
        # than the mistake it corrects -- id (insertion order) is the only
        # thing that reliably tracks "the user's most recent word."
        status = max(terminal, key=lambda e: e["id"])["kind"]
    else:
        status, rank = "applied", 0
        for e in events:
            r = STAGE_RANK.get(e["kind"])
            if r is not None and r > rank:
                status, rank = e["kind"], r

    last_touch = app_row["applied_on"]
    for e in events:
        if e["occurred_on"] > last_touch:
            last_touch = e["occurred_on"]

    return {
        "status": status,
        "is_terminal": status in TERMINAL,
        "stage_rank": STAGE_RANK.get(status, 0),
        "last_touch": last_touch,
        "bdays_quiet": business_days_since(last_touch, today),
        "followups": sum(1 for e in events if e["kind"] == "followup_sent"),
        "had_response": any(e["kind"] in INBOUND for e in events),
        "events": events,
    }


def action_for(d: dict) -> str:
    """The only opinion this tool holds: what to do about this one today."""
    if d["is_terminal"]:
        return "-"
    if d["followups"] == 0 and d["bdays_quiet"] >= FOLLOWUP_AFTER_BUSINESS_DAYS:
        return "FOLLOW UP"
    if d["followups"] >= 1 and d["bdays_quiet"] >= CLOSEOUT_AFTER_BUSINESS_DAYS:
        return "CLOSE OUT"
    return "wait"


def priority_terms(app_row, d: dict) -> list[tuple[str, float]]:
    max_rank = max(STAGE_RANK.values())
    terms = [
        ("base", WEIGHTS["swe_base"] if app_row["lane"] == "swe" else WEIGHTS["other_base"]),
        ("contact", WEIGHTS["contact"] if app_row["contact_email"] else 0.0),
        ("had_response", WEIGHTS["had_response"] if d["had_response"] else 0.0),
        ("stage_progress", WEIGHTS["stage_progress"] * (d["stage_rank"] / max_rank)),
        ("quiet", WEIGHTS["quiet_day"] * d["bdays_quiet"]),
    ]
    closeout = (
        WEIGHTS["closeout"]
        if d["followups"] >= 1 and d["bdays_quiet"] >= CLOSEOUT_AFTER_BUSINESS_DAYS
        else 0.0
    )
    terms.append(("closeout", closeout))
    return terms


def priority_score(app_row, d: dict) -> float:
    return sum(v for _, v in priority_terms(app_row, d))


def pending_reviews(conn) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM review_queue WHERE resolved = 0").fetchone()[0]


def pending_candidates(conn) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM application_candidates WHERE status = 'pending'"
    ).fetchone()[0]


def classify_log_path() -> Path:
    return Path(os.environ.get("JT_LOG_DIR", str(Path.home() / ".jobtrack" / "logs"))) / "jt-classify.jsonl"


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        sys.exit(f"error: {name} must be an integer")


def _parse_created_at(value: str) -> datetime:
    return datetime.fromisoformat(value.replace(" ", "T"))


def _age_days(created_at: str, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    created = _parse_created_at(created_at)
    return max(0, (now.date() - created.date()).days)


def review_queue_stats(conn, now: datetime | None = None) -> dict:
    row = conn.execute(
        """SELECT COUNT(*) AS total, MIN(created_at) AS oldest_created_at
           FROM review_queue
           WHERE resolved = 0"""
    ).fetchone()
    oldest = row["oldest_created_at"]
    age = _age_days(oldest, now) if oldest else 0
    return {"total": row["total"], "oldest_created_at": oldest, "oldest_age_days": age}


def _read_classify_log_events(path: Path, limit: int = 1000) -> list[dict]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()[-limit:]
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("component") == "jt-classify":
            rows.append(row)
    return rows


def classify_failure_stats(log_path: Path) -> dict:
    rows = _read_classify_log_events(log_path)
    since_recovery: list[dict] = []
    consecutive_failures = 0
    for row in rows:
        event = row.get("event")
        if event in CLASSIFY_RECOVERY_EVENTS:
            since_recovery = []
            consecutive_failures = 0
        elif event in CLASSIFY_FAILURE_EVENTS:
            since_recovery.append(row)
            consecutive_failures += 1
    exhaustion = next((r for r in reversed(since_recovery)
                       if r.get("event") == "final_model_exhaustion"), None)
    latest_failure = since_recovery[-1] if since_recovery else None
    return {
        "consecutive_failures": consecutive_failures,
        "has_classifier_exhaustion": exhaustion is not None,
        "latest_exhaustion": exhaustion,
        "latest_failure": latest_failure,
        "log_path": str(log_path),
    }


def review_alerts(conn, *, log_path: Path | None = None, now: datetime | None = None,
                  queue_threshold: int | None = None, age_days: int | None = None,
                  failure_threshold: int | None = None) -> tuple[list[str], dict]:
    queue_threshold = queue_threshold if queue_threshold is not None else _int_env(
        "JT_REVIEW_ALERT_THRESHOLD", DEFAULT_REVIEW_QUEUE_ALERT_THRESHOLD)
    age_days = age_days if age_days is not None else _int_env(
        "JT_REVIEW_AGE_ALERT_DAYS", DEFAULT_REVIEW_AGE_ALERT_DAYS)
    failure_threshold = failure_threshold if failure_threshold is not None else _int_env(
        "JT_CLASSIFY_FAILURE_ALERT_THRESHOLD", DEFAULT_CLASSIFY_FAILURE_ALERT_THRESHOLD)
    log_path = log_path or classify_log_path()

    review_stats = review_queue_stats(conn, now)
    failure_stats = classify_failure_stats(log_path)
    alerts = []
    if review_stats["total"] >= queue_threshold:
        alerts.append(f"total_review_queue_size={review_stats['total']} threshold={queue_threshold}")
    if review_stats["total"] and review_stats["oldest_age_days"] >= age_days:
        alerts.append(
            f"oldest_review_item_age_days={review_stats['oldest_age_days']} threshold={age_days} "
            f"oldest_created_at={review_stats['oldest_created_at']}"
        )
    if failure_stats["has_classifier_exhaustion"]:
        row = failure_stats["latest_exhaustion"] or {}
        alerts.append(
            "classifier_exhaustion_event "
            f"attempt_count={row.get('attempt_count')} status_code={row.get('status_code')} "
            f"error_type={row.get('error_type')}"
        )
    if failure_stats["consecutive_failures"] >= failure_threshold:
        row = failure_stats["latest_failure"] or {}
        alerts.append(
            f"repeated_classify_failures={failure_stats['consecutive_failures']} threshold={failure_threshold} "
            f"latest_event={row.get('event')} status_code={row.get('status_code')} "
            f"error_type={row.get('error_type')}"
        )
    return alerts, {"review": review_stats, "classify": failure_stats}


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def table(headers, rows) -> str:
    if not rows:
        return "  (nothing)"
    cols = [list(map(str, col)) for col in zip(headers, *rows)]
    widths = [max(len(c) for c in col) for col in cols]
    out = ["  " + "  ".join(h.ljust(w) for h, w in zip(headers, widths)),
           "  " + "  ".join("-" * w for w in widths)]
    for r in rows:
        out.append("  " + "  ".join(str(c).ljust(w) for c, w in zip(r, widths)))
    return "\n".join(out)


def review_banner(conn) -> str:
    chunks = []
    n = pending_reviews(conn)
    if n:
        chunks.append(
            f"  !! {n} classification(s) awaiting review. A growing queue means\n"
            f"     the system is silently missing events. Run: jt review\n"
        )
    c = pending_candidates(conn)
    if c:
        chunks.append(
            f"  !! {c} application candidate(s) awaiting decision. A growing queue means\n"
            f"     capture is invisible after creation. Run: jt candidates\n"
        )
    return "\n" + "".join(chunks) if chunks else ""


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def _application_fields(a) -> tuple[str, str, str, str, str | None, str | None, str | None, str | None]:
    day = parse_day(getattr(a, "on", None))
    company = a.company.strip()
    role = a.role.strip()
    lane = a.lane
    if not company or not role:
        sys.exit("error: company and role must be non-empty")
    if lane not in LANES:
        sys.exit(f"error: lane must be one of: {', '.join(LANES)}")
    return (
        company,
        role,
        lane,
        day,
        getattr(a, "source", None),
        getattr(a, "contact", None),
        getattr(a, "url", None),
        getattr(a, "notes", None),
    )


def _find_application_by_key(conn, company: str, role: str, applied_on: str):
    return conn.execute(
        """SELECT * FROM applications
           WHERE company = ? AND role = ? AND applied_on = ?""",
        (company, role, applied_on),
    ).fetchone()


def _insert_application(conn, a) -> tuple[int, tuple]:
    fields = _application_fields(a)
    cur = conn.execute(
        """INSERT INTO applications
           (company, role, lane, applied_on, source, contact_email, url, notes)
           VALUES (?,?,?,?,?,?,?,?)""",
        fields,
    )
    return cur.lastrowid, fields


def cmd_add(conn, a):
    try:
        app_id, fields = _insert_application(conn, a)
        conn.commit()
        company, role, lane, day = fields[:4]
        print(f"#{app_id}  {company} - {role}  [{lane}]  applied {day}")
    except sqlite3.IntegrityError:
        company, role, _, day = _application_fields(a)[:4]
        existing = _find_application_by_key(conn, company, role, day)
        suffix = f" as #{existing['id']}" if existing else ""
        print(f"already logged{suffix}: {company} - {role} on {day}")


def cmd_bulk(conn, a):
    """Read 'Company | Role | lane [| url]' from stdin. For a day's batch."""
    day = parse_day(a.on)
    added = skipped = 0
    for raw in sys.stdin:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            print(f"  skip (need 'Company | Role | lane'): {line}")
            skipped += 1
            continue
        company, role, lane = parts[0], parts[1], parts[2].lower()
        url = parts[3] if len(parts) > 3 else None
        if not company or not role:
            print(f"  skip (empty company/role): {line}")
            skipped += 1
            continue
        if lane not in LANES:
            print(f"  skip (bad lane {lane!r}): {line}")
            skipped += 1
            continue
        try:
            _insert_application(
                conn,
                argparse.Namespace(
                    company=company, role=role, lane=lane, on=day,
                    source=None, contact=None, url=url, notes=None,
                ),
            )
            added += 1
        except sqlite3.IntegrityError:
            skipped += 1
    conn.commit()
    print(f"added {added}, skipped {skipped}")


def cmd_list(conn, a):
    apps = conn.execute(
        "SELECT * FROM applications ORDER BY applied_on DESC, id DESC").fetchall()
    rows = []
    for app in apps:
        d = derive(conn, app)
        if a.lane and app["lane"] != a.lane:
            continue
        if a.status and d["status"] != a.status:
            continue
        if not a.all and d["is_terminal"]:
            continue
        rows.append([app["id"], app["applied_on"], app["company"][:26],
                     app["role"][:32], app["lane"], d["status"],
                     f'{d["bdays_quiet"]}d', action_for(d)])
    print()
    print(table(["#", "APPLIED", "COMPANY", "ROLE", "LANE", "STATUS", "QUIET", "ACTION"], rows))
    print(f"\n  {len(rows)} shown"
          + ("" if a.all else "  (terminal hidden; --all to include)"))
    print(review_banner(conn))


def cmd_stale(conn, a):
    apps = conn.execute("SELECT * FROM applications").fetchall()
    rows = []
    for app in apps:
        d = derive(conn, app)
        act = action_for(d)
        if act in ("FOLLOW UP", "CLOSE OUT"):
            rows.append([app["id"], app["company"][:26], app["role"][:32],
                         d["status"], f'{d["bdays_quiet"]}d', act,
                         app["contact_email"] or "-"])
    rows.sort(key=lambda r: (r[5] != "FOLLOW UP", -int(r[4][:-1])))
    print(review_banner(conn))
    print(table(["#", "COMPANY", "ROLE", "STATUS", "QUIET", "ACTION", "CONTACT"], rows))
    print()


def cmd_priority(conn, a):
    today = date.fromisoformat(a.today) if a.today else None
    if a.explain:
        app = conn.execute("SELECT * FROM applications WHERE id = ?", (a.explain,)).fetchone()
        if not app:
            sys.exit(f"error: no application #{a.explain}")
        d = derive(conn, app, today)
        rows = [[name, f"{value:.2f}"] for name, value in priority_terms(app, d)]
        rows.append(["TOTAL", f"{priority_score(app, d):.2f}"])
        print(f"\n  #{app['id']}  {app['company']} - {app['role']}  ({d['status']})")
        print(table(["TERM", "POINTS"], rows))
        print(f"\n  action: {action_for(d)}\n")
        return

    rows = []
    for app in conn.execute("SELECT * FROM applications ORDER BY applied_on, id"):
        d = derive(conn, app, today)
        if d["is_terminal"]:
            continue
        rows.append([priority_score(app, d), app, d])
    rows.sort(key=lambda r: r[0], reverse=True)
    out = [[app["id"], f"{score:.2f}", app["company"][:26], app["role"][:32],
            d["status"], action_for(d)] for score, app, d in rows[:10]]
    print()
    print(table(["#", "SCORE", "COMPANY", "ROLE", "STATUS", "ACTION"], out))
    print()


def cmd_log(conn, a):
    app = conn.execute("SELECT * FROM applications WHERE id = ?", (a.id,)).fetchone()
    if not app:
        sys.exit(f"error: no application #{a.id}")
    # `note` is a human act, deliberately outside the transition gate (see
    # transitions.py) -- always allowed, terminal or not, since it never
    # changes status. Everything else goes through the same legality check
    # that binds the classifier, so a manual `jt log` can't silently do what
    # a confidently-wrong model is forbidden from doing.
    if a.kind != "note" and not a.force:
        current = derive(conn, app)["status"]
        ok, reason = is_legal(current, a.kind)
        if not ok:
            sys.exit(f"error: {reason}\n  pass --force to log it anyway (for a deliberate manual correction)")
    try:
        conn.execute(
            "INSERT INTO events (application_id, occurred_on, kind, evidence) "
            "VALUES (?,?,?,?)",
            (a.id, parse_day(a.on), a.kind, a.note),
        )
    except sqlite3.IntegrityError as e:
        sys.exit(f"error: {e}\n  valid kinds: {', '.join(sorted(EVENT_KINDS))}")
    conn.commit()
    d = derive(conn, app)
    print(f"#{a.id} {app['company']} - {app['role']}  ->  {d['status']}")


def cmd_show(conn, a):
    app = conn.execute("SELECT * FROM applications WHERE id = ?", (a.id,)).fetchone()
    if not app:
        sys.exit(f"error: no application #{a.id}")
    d = derive(conn, app)
    print(f"\n  #{app['id']}  {app['company']} - {app['role']}")
    print(f"  lane {app['lane']} | applied {app['applied_on']} | status {d['status']}")
    print(f"  quiet {d['bdays_quiet']} business days | follow-ups sent {d['followups']}")
    if app["contact_email"]:
        print(f"  contact  {app['contact_email']}")
    if app["url"]:
        print(f"  url      {app['url']}")
    print(f"  action   {action_for(d)}\n")
    rows = [[e["occurred_on"], e["kind"],
             "auto" if e["gmail_msg_id"] else "manual",
             (e["evidence"] or "")[:52]] for e in d["events"]]
    print(table(["WHEN", "KIND", "SOURCE", "EVIDENCE"], rows))
    print()


def cmd_rm(conn, a):
    """Deletable only while it has zero events. Past that, it is history."""
    app = conn.execute("SELECT * FROM applications WHERE id = ?", (a.id,)).fetchone()
    if not app:
        sys.exit(f"error: no application #{a.id}")
    n = conn.execute("SELECT COUNT(*) FROM events WHERE application_id = ?",
                     (a.id,)).fetchone()[0]
    if n:
        sys.exit(f"error: #{a.id} has {n} event(s) and cannot be deleted.\n"
                 f"  History is not erasable. Use: jt log {a.id} withdrawn")
    conn.execute("DELETE FROM applications WHERE id = ?", (a.id,))
    conn.commit()
    print(f"removed #{a.id} {app['company']} - {app['role']}")


def _write_csv(conn, writer, lane=None) -> int:
    writer.writerow(["Applied", "Company", "Role", "Lane", "Status", "Link"])
    n = 0
    for app in conn.execute("SELECT * FROM applications ORDER BY applied_on"):
        if lane and app["lane"] != lane:
            continue
        d = derive(conn, app)
        writer.writerow([app["applied_on"], app["company"], app["role"],
                         app["lane"], d["status"], app["url"] or ""])
        n += 1
    return n


def cmd_export(conn, a):
    if a.out:
        with open(a.out, "w", newline="") as f:
            n = _write_csv(conn, csv.writer(f), a.lane)
        print(f"wrote {n} rows to {a.out}")
    else:
        _write_csv(conn, csv.writer(sys.stdout), a.lane)


def cmd_backup(conn, a):
    """The likeliest catastrophic failure here is not a race condition. It is
    `rm -rf ~/.jobtrack`. Put this in cron."""
    dest = Path(a.dir).expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    stamp = date.today().isoformat()
    db_copy = dest / f"jobs-{stamp}.db"
    target = sqlite3.connect(db_copy)
    try:
        conn.backup(target)      # consistent snapshot, safe on a live db
    finally:
        target.close()
    csv_copy = dest / f"jobs-{stamp}.csv"
    with open(csv_copy, "w", newline="") as f:
        _write_csv(conn, csv.writer(f))
    print(f"backed up to {db_copy} and {csv_copy}")


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _default_source_ref(source: str, raw_input: str, source_ref: str | None) -> str | None:
    source_ref = _clean_optional(source_ref)
    if source_ref:
        return source_ref
    if not raw_input:
        return None
    digest = hashlib.sha256(raw_input.encode("utf-8")).hexdigest()
    return f"{source}:{digest}"


def insert_application_candidate(
    conn,
    *,
    raw_input: str,
    source: str = "manual_capture",
    source_ref: str | None = None,
    company: str | None = None,
    role: str | None = None,
    lane: str | None = None,
    applied_on: str | None = None,
    contact_email: str | None = None,
    url: str | None = None,
    notes: str | None = None,
    commit: bool = True,
) -> tuple[int | None, bool]:
    source = _clean_optional(source) or "manual_capture"
    source_ref = _default_source_ref(source, raw_input, source_ref)
    company = _clean_optional(company)
    role = _clean_optional(role)
    lane = _clean_optional(lane)
    contact_email = _clean_optional(contact_email)
    url = _clean_optional(url)
    notes = _clean_optional(notes)
    if applied_on:
        applied_on = parse_day(applied_on)
    if lane and lane not in LANES:
        sys.exit(f"error: lane must be one of: {', '.join(LANES)}")

    cur = conn.execute(
        """INSERT OR IGNORE INTO application_candidates
           (raw_input, source, source_ref, company, role, lane, applied_on,
            contact_email, url, notes)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (raw_input, source, source_ref, company, role, lane, applied_on,
        contact_email, url, notes),
    )
    if commit:
        conn.commit()
    if cur.rowcount:
        return cur.lastrowid, True
    existing = None
    if source_ref:
        existing = conn.execute(
            "SELECT id FROM application_candidates WHERE source_ref = ?",
            (source_ref,),
        ).fetchone()
    if existing is None and url:
        existing = conn.execute(
            "SELECT id FROM application_candidates WHERE url = ?",
            (url,),
        ).fetchone()
    return (existing["id"] if existing else None), False


def cmd_capture(conn, a):
    raw_input = " ".join(a.text) if a.text else sys.stdin.read()
    candidate_id, inserted = insert_application_candidate(
        conn,
        raw_input=raw_input,
        source=a.source,
        source_ref=a.source_ref,
        company=a.company,
        role=a.role,
        lane=a.lane,
        applied_on=a.on,
        contact_email=a.contact,
        url=a.url,
        notes=a.notes,
    )
    if inserted:
        print(f"captured candidate #{candidate_id}")
    else:
        print(f"already captured candidate #{candidate_id}; no-op")


def _candidate_application_args(candidate, a):
    return argparse.Namespace(
        company=a.company if a.company is not None else (candidate["company"] or ""),
        role=a.role if a.role is not None else (candidate["role"] or ""),
        lane=a.lane if a.lane is not None else (candidate["lane"] or ""),
        on=a.on if a.on is not None else candidate["applied_on"],
        source=a.source if a.source is not None else candidate["source"],
        contact=a.contact if a.contact is not None else candidate["contact_email"],
        url=a.url if a.url is not None else candidate["url"],
        notes=a.notes if a.notes is not None else candidate["notes"],
    )


def cmd_candidate_accept(conn, a):
    candidate = conn.execute(
        "SELECT * FROM application_candidates WHERE id = ?", (a.candidate_id,)
    ).fetchone()
    if not candidate:
        sys.exit(f"error: no application candidate #{a.candidate_id}")
    if candidate["status"] == "accepted":
        print(
            f"candidate #{a.candidate_id} already accepted"
            + (f" as application #{candidate['accepted_application_id']}"
               if candidate["accepted_application_id"] else "")
        )
        return
    if candidate["status"] == "rejected":
        print(f"candidate #{a.candidate_id} already rejected")
        return

    app_args = _candidate_application_args(candidate, a)
    company, role, _, day = _application_fields(app_args)[:4]
    existing = _find_application_by_key(conn, company, role, day)
    if existing:
        sys.exit(f"error: matching application already exists as #{existing['id']}")

    conn.commit()
    old_isolation = conn.isolation_level
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            """UPDATE application_candidates
               SET status = 'accepted', decided_at = datetime('now')
               WHERE id = ? AND status = 'pending'""",
            (a.candidate_id,),
        )
        if cur.rowcount == 0:
            conn.execute("COMMIT")
            print(f"candidate #{a.candidate_id} was already decided")
            return
        app_id, fields = _insert_application(conn, app_args)
        conn.execute(
            """UPDATE application_candidates
               SET accepted_application_id = ?
               WHERE id = ?""",
            (app_id, a.candidate_id),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.isolation_level = old_isolation

    company, role, lane, day = fields[:4]
    print(f"accepted candidate #{a.candidate_id} as #{app_id}  {company} - {role}  [{lane}]  applied {day}")


def cmd_candidate_reject(conn, a):
    candidate = conn.execute(
        "SELECT * FROM application_candidates WHERE id = ?", (a.candidate_id,)
    ).fetchone()
    if not candidate:
        sys.exit(f"error: no application candidate #{a.candidate_id}")
    cur = conn.execute(
        """UPDATE application_candidates
           SET status = 'rejected',
               rejected_note = ?,
               decided_at = datetime('now')
           WHERE id = ? AND status = 'pending'""",
        (a.note, a.candidate_id),
    )
    conn.commit()
    if cur.rowcount == 0:
        print(f"candidate #{a.candidate_id} was already decided")
    else:
        print(f"rejected candidate #{a.candidate_id}")


def cmd_candidates(conn, a):
    if getattr(a, "candidate_cmd", None) == "accept":
        return cmd_candidate_accept(conn, a)
    if getattr(a, "candidate_cmd", None) == "reject":
        return cmd_candidate_reject(conn, a)
    rows = conn.execute(
        """SELECT * FROM application_candidates
           WHERE status = 'pending'
           ORDER BY id"""
    ).fetchall()
    out = []
    for r in rows:
        preview = (r["raw_input"] or "").replace("\n", " ")[:36]
        out.append([
            r["id"], r["created_at"][:10], r["source"],
            (r["company"] or "-")[:24], (r["role"] or "-")[:28],
            r["lane"] or "-", r["applied_on"] or "-", r["url"] or preview,
        ])
    print()
    print(table(["#", "CREATED", "SOURCE", "COMPANY", "ROLE", "LANE", "APPLIED", "REF"], out))
    print(f"\n  {len(rows)} pending candidate(s)\n")


def cmd_review(conn, a):
    if getattr(a, "review_cmd", None) == "stale":
        return cmd_review_stale(conn, a)
    rows = conn.execute(
        "SELECT * FROM review_queue WHERE resolved = 0 ORDER BY id").fetchall()
    if not rows:
        print("\n  review queue empty\n")
        return
    print()
    for r in rows:
        print(f"  [{r['id']}] {r['reason']}\n      {r['proposed_json']}")
    print(f"\n  {len(rows)} pending. Resolve with: jt log <app_id> <kind>, "
          f"then jt resolve <queue_id>\n")


def cmd_review_stale(conn, a):
    alerts, stats = review_alerts(
        conn,
        log_path=Path(a.log) if a.log else None,
        queue_threshold=a.queue_threshold,
        age_days=a.age_days,
        failure_threshold=a.failure_threshold,
    )
    review = stats["review"]
    classify = stats["classify"]
    print(
        "review health: "
        f"total={review['total']} "
        f"oldest_age_days={review['oldest_age_days']} "
        f"oldest_created_at={review['oldest_created_at'] or '-'}"
    )
    print(
        "classify health: "
        f"consecutive_failures={classify['consecutive_failures']} "
        f"classifier_exhaustion={'yes' if classify['has_classifier_exhaustion'] else 'no'} "
        f"log={classify['log_path']}"
    )
    if alerts:
        for alert in alerts:
            print(f"ALERT {alert}")
        if not a.no_fail:
            sys.exit(1)
        print("review health alerts present")
        return
    print("review health OK")


def cmd_resolve(conn, a):
    row = conn.execute(
        "SELECT * FROM review_queue WHERE id = ?", (a.queue_id,)).fetchone()
    if not row:
        sys.exit(f"error: no review queue item #{a.queue_id}")
    if a.event_id:
        event = conn.execute("SELECT id FROM events WHERE id = ?", (a.event_id,)).fetchone()
        if not event:
            sys.exit(f"error: no event #{a.event_id}")
    cur = conn.execute(
        """UPDATE review_queue
           SET resolved = 1,
               resolved_event_id = ?,
               resolved_note = ?,
               resolved_at = datetime('now')
           WHERE id = ? AND resolved = 0""",
        (a.event_id, a.note, a.queue_id),
    )
    conn.commit()
    if cur.rowcount == 0:
        print(f"queue item {a.queue_id} was already resolved")
    else:
        print(f"resolved queue item {a.queue_id}")


def cmd_where(conn, a):
    print(db_path())


# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="jt", description="job application tracker")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("add", help="log one application")
    s.add_argument("company"); s.add_argument("role")
    s.add_argument("--lane", required=True, choices=LANES)
    s.add_argument("--on"); s.add_argument("--source")
    s.add_argument("--contact"); s.add_argument("--url"); s.add_argument("--notes")
    s.set_defaults(fn=cmd_add)

    s = sub.add_parser("bulk", help="stdin: 'Company | Role | lane [| url]' per line")
    s.add_argument("--on"); s.set_defaults(fn=cmd_bulk)

    s = sub.add_parser("list", help="show open applications")
    s.add_argument("--lane", choices=LANES); s.add_argument("--status")
    s.add_argument("--all", action="store_true"); s.set_defaults(fn=cmd_list)

    s = sub.add_parser("stale", help="what needs action today")
    s.set_defaults(fn=cmd_stale)

    s = sub.add_parser("priority", help="rank open applications by next-action value")
    s.add_argument("--explain", type=int, metavar="ID")
    s.add_argument("--today", help=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_priority)

    s = sub.add_parser("log", help="append an event")
    s.add_argument("id", type=int)
    s.add_argument("kind", choices=sorted(EVENT_KINDS))
    s.add_argument("--on"); s.add_argument("--note")
    s.add_argument("--force", action="store_true",
                    help="log even if illegal per transitions.py (deliberate manual correction)")
    s.set_defaults(fn=cmd_log)

    s = sub.add_parser("show", help="full history for one application")
    s.add_argument("id", type=int); s.set_defaults(fn=cmd_show)

    s = sub.add_parser("rm", help="delete an application (only if it has no events)")
    s.add_argument("id", type=int); s.set_defaults(fn=cmd_rm)

    s = sub.add_parser("export", help="CSV out (Pursuit: --lane swe)")
    s.add_argument("--lane", choices=LANES); s.add_argument("--out")
    s.set_defaults(fn=cmd_export)

    s = sub.add_parser("backup", help="snapshot db + csv to a directory")
    s.add_argument("dir"); s.set_defaults(fn=cmd_backup)

    s = sub.add_parser("capture", help="capture a possible application from stdin/text")
    s.add_argument("text", nargs="*")
    s.add_argument("--source", default="manual_capture")
    s.add_argument("--source-ref")
    s.add_argument("--company")
    s.add_argument("--role")
    s.add_argument("--lane", choices=LANES)
    s.add_argument("--on")
    s.add_argument("--contact")
    s.add_argument("--url")
    s.add_argument("--notes")
    s.set_defaults(fn=cmd_capture)

    s = sub.add_parser("candidates", help="pending captured application candidates")
    cand = s.add_subparsers(dest="candidate_cmd")
    s.set_defaults(fn=cmd_candidates)

    c = cand.add_parser("accept", help="accept one candidate as an application")
    c.add_argument("candidate_id", type=int)
    c.add_argument("company", nargs="?")
    c.add_argument("role", nargs="?")
    c.add_argument("--lane", choices=LANES)
    c.add_argument("--on")
    c.add_argument("--source")
    c.add_argument("--contact")
    c.add_argument("--url")
    c.add_argument("--notes")
    c.set_defaults(fn=cmd_candidates)

    c = cand.add_parser("reject", help="reject one candidate")
    c.add_argument("candidate_id", type=int)
    c.add_argument("--note")
    c.set_defaults(fn=cmd_candidates)

    s = sub.add_parser("review", help="pending classifier proposals (Phase 3)")
    s.add_argument("review_cmd", nargs="?", choices=["stale"])
    s.add_argument("--queue-threshold", type=int, default=None)
    s.add_argument("--age-days", type=int, default=None)
    s.add_argument("--failure-threshold", type=int, default=None)
    s.add_argument("--log")
    s.add_argument("--no-fail", action="store_true")
    s.set_defaults(fn=cmd_review)

    s = sub.add_parser("resolve", help="mark a review item handled")
    s.add_argument("queue_id", type=int)
    s.add_argument("--event-id", type=int)
    s.add_argument("--note")
    s.set_defaults(fn=cmd_resolve)

    s = sub.add_parser("where", help="print database path")
    s.set_defaults(fn=cmd_where)
    return p


def main():
    a = build_parser().parse_args()
    conn = connect()
    try:
        a.fn(conn, a)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
