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
import os
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

SCHEMA_VERSION = 3

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
    """v1 shipped events with ON DELETE CASCADE and no kind FK."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='events'"
    ).fetchone()
    return bool(row and "ON DELETE CASCADE" in (row[0] or ""))


def _migrate_v1_to_v2(conn) -> None:
    """Rebuild events with RESTRICT + kind FK, preserving every row."""
    conn.executescript("ALTER TABLE events RENAME TO events_legacy;")
    conn.executescript(EVENTS_DDL)
    conn.execute("""
        INSERT INTO events
            (id, application_id, occurred_on, kind, gmail_msg_id,
             confidence, evidence, created_at)
        SELECT id, application_id, occurred_on, kind, gmail_msg_id,
               confidence, evidence, created_at
        FROM events_legacy
    """)
    conn.executescript("DROP TABLE events_legacy;")
    conn.commit()
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
        status = terminal[-1]["kind"]
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
    n = pending_reviews(conn)
    if not n:
        return ""
    return (f"\n  !! {n} classification(s) awaiting review. A growing queue means\n"
            f"     the system is silently missing events. Run: jt review\n")


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_add(conn, a):
    day = parse_day(a.on)
    company = a.company.strip()
    role = a.role.strip()
    if not company or not role:
        sys.exit("error: company and role must be non-empty")
    try:
        cur = conn.execute(
            """INSERT INTO applications
               (company, role, lane, applied_on, source, contact_email, url, notes)
               VALUES (?,?,?,?,?,?,?,?)""",
            (company, role, a.lane, day,
             a.source, a.contact, a.url, a.notes),
        )
        conn.commit()
        print(f"#{cur.lastrowid}  {company} - {role}  [{a.lane}]  applied {day}")
    except sqlite3.IntegrityError:
        print(f"already logged: {company} - {role} on {day}")


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
            conn.execute(
                "INSERT INTO applications (company, role, lane, applied_on, url) "
                "VALUES (?,?,?,?,?)",
                (company, role, lane, day, url),
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


def cmd_review(conn, a):
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
    s.add_argument("--on"); s.add_argument("--note"); s.set_defaults(fn=cmd_log)

    s = sub.add_parser("show", help="full history for one application")
    s.add_argument("id", type=int); s.set_defaults(fn=cmd_show)

    s = sub.add_parser("rm", help="delete an application (only if it has no events)")
    s.add_argument("id", type=int); s.set_defaults(fn=cmd_rm)

    s = sub.add_parser("export", help="CSV out (Pursuit: --lane swe)")
    s.add_argument("--lane", choices=LANES); s.add_argument("--out")
    s.set_defaults(fn=cmd_export)

    s = sub.add_parser("backup", help="snapshot db + csv to a directory")
    s.add_argument("dir"); s.set_defaults(fn=cmd_backup)

    s = sub.add_parser("review", help="pending classifier proposals (Phase 3)")
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
