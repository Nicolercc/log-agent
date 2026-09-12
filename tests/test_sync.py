from datetime import date, datetime, timezone
import json

import pytest

import jt
import sync


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("JT_DB", str(tmp_path / "test.db"))
    c = jt.connect()
    yield c
    c.close()


def b64(s):
    import base64

    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def ms(day):
    dt = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
    return str(int(dt.timestamp() * 1000))


def message(msg_id, day="2026-08-12", sender="jobs@example.com",
            subject="Thanks", body="We received your application."):
    return {
        "id": msg_id,
        "internalDate": ms(day),
        "payload": {
            "headers": [
                {"name": "From", "value": sender},
                {"name": "Subject", "value": subject},
            ],
            "mimeType": "text/plain",
            "body": {"data": b64(body)},
        },
    }


class Request:
    def __init__(self, value=None, exc=None):
        self.value = value
        self.exc = exc

    def execute(self):
        if self.exc:
            raise self.exc
        return self.value


class Messages:
    def __init__(self, pages, messages, fail_get=None):
        self.pages = list(pages)
        self.messages = messages
        self.fail_get = fail_get or {}
        self.queries = []

    def list(self, **kwargs):
        self.queries.append(kwargs["q"])
        return Request(self.pages.pop(0))

    def get(self, **kwargs):
        msg_id = kwargs["id"]
        if msg_id in self.fail_get:
            return Request(exc=self.fail_get[msg_id])
        return Request(self.messages[msg_id])


class Users:
    def __init__(self, messages):
        self._messages = messages

    def messages(self):
        return self._messages


class Service:
    def __init__(self, pages, messages, fail_get=None):
        self.messages_obj = Messages(pages, messages, fail_get)

    def users(self):
        return Users(self.messages_obj)


def fake_google_clients():
    class Request:
        pass

    class Credentials:
        @staticmethod
        def from_authorized_user_file(path, scopes):
            raise AssertionError("token should not be loaded")

    class Flow:
        pass

    class WSGITimeoutError(Exception):
        pass

    def build(*args, **kwargs):
        raise AssertionError("service should not be built")

    return Request, Credentials, Flow, WSGITimeoutError, build


def test_unattended_sync_fails_fast_when_token_is_missing(tmp_path, monkeypatch):
    client = tmp_path / "client.json"
    client.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("JT_UNATTENDED", "1")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT", str(client))
    monkeypatch.setenv("GOOGLE_TOKEN_PATH", str(tmp_path / "missing-token.json"))
    monkeypatch.setattr(sync, "_require_google_clients", fake_google_clients)

    with pytest.raises(SystemExit, match="GOOGLE_TOKEN_PATH does not exist"):
        sync.build_gmail_service()


def test_google_client_path_must_be_absolute(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT", "relative-client.json")
    monkeypatch.setattr(sync, "_require_google_clients", fake_google_clients)

    with pytest.raises(
        SystemExit, match="GOOGLE_OAUTH_CLIENT must be an absolute path"
    ):
        sync.build_gmail_service()


def test_oauth_timeout_defaults_to_five_minutes(monkeypatch):
    monkeypatch.delenv("GOOGLE_OAUTH_TIMEOUT_SECONDS", raising=False)
    assert sync.oauth_timeout_seconds() == 300


def test_oauth_timeout_can_be_disabled(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_TIMEOUT_SECONDS", "0")
    assert sync.oauth_timeout_seconds() is None


def test_oauth_timeout_must_be_an_integer(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_TIMEOUT_SECONDS", "soon")
    with pytest.raises(
        SystemExit, match="GOOGLE_OAUTH_TIMEOUT_SECONDS must be an integer"
    ):
        sync.oauth_timeout_seconds()


def test_sync_structured_log_writes_jsonl(tmp_path, monkeypatch):
    log_path = tmp_path / "sync.jsonl"
    monkeypatch.setattr(sync, "LOG_PATH", log_path)

    sync._structured_log("success", exit_status=0, seen=2, inserted=1)

    row = json.loads(log_path.read_text(encoding="utf-8"))
    assert row["component"] == "jt-sync"
    assert row["event"] == "success"
    assert row["exit_status"] == 0
    assert row["seen"] == 2


def test_duplicate_message_is_a_noop(conn):
    svc = Service(
        [{"messages": [{"id": "m1"}]}],
        {"m1": message("m1")},
    )
    seen, inserted = sync.sync_messages(conn, svc, since="2026-08-01")
    assert (seen, inserted) == (1, 1)

    svc = Service(
        [{"messages": [{"id": "m1"}]}],
        {"m1": message("m1")},
    )
    seen, inserted = sync.sync_messages(conn, svc, since="2026-08-01")
    assert (seen, inserted) == (1, 0)
    assert conn.execute("SELECT COUNT(*) FROM raw_messages").fetchone()[0] == 1


def test_watermark_does_not_advance_on_mid_run_exception(conn):
    class Boom(Exception):
        pass

    svc = Service(
        [{"messages": [{"id": "m1"}, {"id": "m2"}]}],
        {"m1": message("m1"), "m2": message("m2")},
        fail_get={"m2": Boom("network broke")},
    )

    with pytest.raises(Boom):
        sync.sync_messages(conn, svc, since="2026-08-01")

    assert conn.execute("SELECT COUNT(*) FROM raw_messages").fetchone()[0] == 0
    assert conn.execute(
        "SELECT value FROM sync_state WHERE key = 'gmail_last_synced'"
    ).fetchone() is None


def test_pagination_fetches_all_pages(conn):
    svc = Service(
        [
            {"messages": [{"id": "m1"}], "nextPageToken": "next"},
            {"messages": [{"id": "m2"}]},
        ],
        {"m1": message("m1"), "m2": message("m2")},
    )

    seen, inserted = sync.sync_messages(conn, svc, since="2026-08-01")
    assert (seen, inserted) == (2, 2)
    assert conn.execute("SELECT COUNT(*) FROM raw_messages").fetchone()[0] == 2


def test_overlap_window_refetches_without_duplication(conn, monkeypatch):
    conn.execute(
        "INSERT INTO sync_state (key, value) VALUES ('gmail_last_synced', '2026-08-12')"
    )
    conn.commit()
    monkeypatch.setenv("GMAIL_OVERLAP_DAYS", "2")
    svc = Service(
        [{"messages": [{"id": "m1"}]}],
        {"m1": message("m1")},
    )

    sync.sync_messages(conn, svc, today=date(2026, 8, 20))
    assert "after:2026/08/10" in svc.messages_obj.queries[0]

    svc = Service(
        [{"messages": [{"id": "m1"}]}],
        {"m1": message("m1")},
    )
    _, inserted = sync.sync_messages(conn, svc, today=date(2026, 8, 20))
    assert inserted == 0


def test_watermark_uses_the_run_wide_max_not_the_last_page(conn):
    """Gmail lists newest-first by default, so the last page fetched is
    usually the OLDEST. Writing the watermark per-page (using only that
    page's own max) lets the final page silently roll it backward even on a
    clean run. The watermark must reflect the max received_on across every
    page, and must be written once, after the whole run completes."""
    svc = Service(
        [
            {"messages": [{"id": "m1"}], "nextPageToken": "next"},  # newest page, first
            {"messages": [{"id": "m2"}]},                             # oldest page, last
        ],
        {"m1": message("m1", day="2026-08-10"), "m2": message("m2", day="2026-08-01")},
    )
    sync.sync_messages(conn, svc, since="2026-07-01", today=date(2026, 8, 12))
    watermark = conn.execute(
        "SELECT value FROM sync_state WHERE key = 'gmail_last_synced'"
    ).fetchone()[0]
    assert watermark == "2026-08-10"


def test_empty_sync_advances_watermark_to_run_day(conn):
    svc = Service(
        [{"messages": []}],
        {},
    )

    seen, inserted = sync.sync_messages(
        conn, svc, since="2026-08-01", today=date(2026, 8, 20))

    assert (seen, inserted) == (0, 0)
    assert conn.execute(
        "SELECT value FROM sync_state WHERE key = 'gmail_last_synced'"
    ).fetchone()[0] == "2026-08-20"
