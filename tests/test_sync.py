from datetime import date, datetime, timezone

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
