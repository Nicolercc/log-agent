#!/usr/bin/env python3
"""
sync.py -- Gmail ingestion for jt.

Phase 2 deliberately parses nothing about the job search. It only lands raw
messages in `raw_messages`, with an overlap window and database-owned
idempotency. Classification is a separate phase.
"""

import argparse
import base64
import html
import os
import random
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import jt

GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
DEFAULT_LABEL = "jobsearch"
DEFAULT_OVERLAP_DAYS = 2
DEFAULT_LOOKBACK_DAYS = 30
MAX_BODY_CHARS = 4000
MAX_ATTEMPTS = 5


@dataclass(frozen=True)
class RawMessage:
    gmail_msg_id: str
    received_on: str | None
    sender: str | None
    subject: str | None
    body: str


def _require_google_clients():
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as e:
        raise SystemExit(
            "error: Gmail sync requires the gmail extra:\n"
            "  pipx install -e '.[gmail]'\n"
            f"missing import: {e.name}"
        ) from e
    return Request, Credentials, InstalledAppFlow, build


def build_gmail_service():
    Request, Credentials, InstalledAppFlow, build = _require_google_clients()
    client_path = os.environ.get("GOOGLE_OAUTH_CLIENT")
    if not client_path:
        raise SystemExit("error: set GOOGLE_OAUTH_CLIENT to your OAuth client JSON")
    token_path = Path(os.environ.get("GOOGLE_TOKEN_PATH", "~/.jobtrack/token.json")).expanduser()
    creds = None
    if token_path.exists():
        token_path.chmod(0o600)
        creds = Credentials.from_authorized_user_file(str(token_path), [GMAIL_SCOPE])
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(client_path, [GMAIL_SCOPE])
            creds = flow.run_local_server(port=0)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json(), encoding="utf-8")
        token_path.chmod(0o600)
    return build("gmail", "v1", credentials=creds)


def _status_code(exc: Exception) -> int | None:
    resp = getattr(exc, "resp", None)
    status = getattr(resp, "status", None)
    if status is None:
        status = getattr(exc, "status_code", None)
    return int(status) if status is not None else None


def execute_with_retry(request, sleep=time.sleep, rand=random.random):
    delay = 1.0
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return request.execute()
        except Exception as e:
            status = _status_code(e)
            if status not in {429, 500, 502, 503, 504} or attempt == MAX_ATTEMPTS:
                raise
            sleep(delay + rand())
            delay *= 2


def _decode_body(data: str | None) -> str:
    if not data:
        return ""
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8", "replace")


def _html_to_text(s: str) -> str:
    s = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _payload_parts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    parts = [payload]
    out = []
    while parts:
        part = parts.pop(0)
        out.append(part)
        parts.extend(part.get("parts") or [])
    return out


def extract_body(payload: dict[str, Any]) -> str:
    text_html = ""
    for part in _payload_parts(payload):
        mime = part.get("mimeType")
        data = (part.get("body") or {}).get("data")
        if mime == "text/plain" and data:
            return _decode_body(data)[:MAX_BODY_CHARS]
        if mime == "text/html" and data and not text_html:
            text_html = _decode_body(data)
    if text_html:
        return _html_to_text(text_html)[:MAX_BODY_CHARS]
    return ""


def _headers(payload: dict[str, Any]) -> dict[str, str]:
    return {
        h.get("name", "").lower(): h.get("value", "")
        for h in payload.get("headers") or []
    }


def _received_day(message: dict[str, Any]) -> str | None:
    raw = message.get("internalDate")
    if raw is None:
        return None
    try:
        seconds = int(raw) / 1000
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(seconds, tz=timezone.utc).date().isoformat()


def parse_message(message: dict[str, Any]) -> RawMessage:
    payload = message.get("payload") or {}
    headers = _headers(payload)
    return RawMessage(
        gmail_msg_id=message["id"],
        received_on=_received_day(message),
        sender=headers.get("from"),
        subject=headers.get("subject"),
        body=extract_body(payload),
    )


def _watermark(conn) -> str | None:
    row = conn.execute(
        "SELECT value FROM sync_state WHERE key = 'gmail_last_synced'"
    ).fetchone()
    return row[0] if row else None


def start_day(conn, since: str | None, today: date | None = None) -> str:
    if since:
        return jt.parse_day(since, today)
    today = today or date.today()
    mark = _watermark(conn)
    if mark:
        return (date.fromisoformat(mark) - timedelta(days=overlap_days())).isoformat()
    return (today - timedelta(days=DEFAULT_LOOKBACK_DAYS)).isoformat()


def overlap_days() -> int:
    try:
        return int(os.environ.get("GMAIL_OVERLAP_DAYS", DEFAULT_OVERLAP_DAYS))
    except ValueError:
        raise SystemExit("error: GMAIL_OVERLAP_DAYS must be an integer")


def gmail_query(label: str, since_day: str) -> str:
    return f"label:{label} after:{since_day.replace('-', '/')}"


def insert_rows(conn, rows: list[RawMessage]) -> int:
    inserted = 0
    for row in rows:
        cur = conn.execute(
            """INSERT OR IGNORE INTO raw_messages
               (gmail_msg_id, received_on, sender, subject, body, processed)
               VALUES (?,?,?,?,?,0)""",
            (row.gmail_msg_id, row.received_on, row.sender, row.subject, row.body),
        )
        inserted += cur.rowcount
    return inserted


def set_watermark(conn, day: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO sync_state (key, value) VALUES ('gmail_last_synced', ?)",
        (day,),
    )


def sync_messages(conn, service, *, since: str | None = None, dry_run: bool = False,
                  today: date | None = None, sleep=time.sleep) -> tuple[int, int]:
    label = os.environ.get("GMAIL_LABEL", DEFAULT_LABEL)
    run_day = today or date.today()
    since_day = start_day(conn, since, run_day)
    query = gmail_query(label, since_day)
    page_token = None
    inserted = seen = 0

    while True:
        req = service.users().messages().list(
            userId="me", q=query, pageToken=page_token)
        page = execute_with_retry(req, sleep=sleep)
        ids = [m["id"] for m in page.get("messages", [])]
        batch: list[RawMessage] = []
        for msg_id in ids:
            req = service.users().messages().get(userId="me", id=msg_id, format="full")
            batch.append(parse_message(execute_with_retry(req, sleep=sleep)))
        seen += len(batch)

        if dry_run:
            for row in batch:
                print(f"{row.received_on or '-'}  {row.sender or '-'}  {row.subject or '-'}")
        else:
            inserted += insert_rows(conn, batch)
            mark = max((r.received_on for r in batch if r.received_on),
                       default=run_day.isoformat())
            set_watermark(conn, mark)
            conn.commit()

        page_token = page.get("nextPageToken")
        if not page_token:
            break

    return seen, inserted


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="sync Gmail jobsearch mail into jt")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--since", help="override watermark with YYYY-MM-DD")
    return p


def main() -> None:
    args = build_parser().parse_args()
    conn = jt.connect()
    try:
        seen, inserted = sync_messages(
            conn, build_gmail_service(), since=args.since, dry_run=args.dry_run)
    finally:
        conn.close()
    if args.dry_run:
        print(f"would inspect {seen} message(s)")
    else:
        print(f"inspected {seen}, inserted {inserted}")


if __name__ == "__main__":
    main()
