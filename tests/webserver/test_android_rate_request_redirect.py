from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.webserver import android_rate_request


class _Result:
    def __init__(self, value=None):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _Db:
    def __init__(self, results=None, error=None):
        self.results = list(results or [])
        self.error = error
        self.added = []
        self.commit = AsyncMock()
        self.rollback = AsyncMock()

    async def execute(self, _statement):
        if self.error:
            raise self.error
        return _Result(self.results.pop(0) if self.results else None)

    def add(self, value):
        self.added.append(value)


class _SessionFactory:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, *_args):
        return None


def _patch_session(monkeypatch, db):
    monkeypatch.setattr(
        android_rate_request,
        "AsyncSessionLocal",
        lambda: _SessionFactory(db),
    )


async def test_android_rate_request_redirect_records_first_click(monkeypatch):
    notification = SimpleNamespace(id=42, user_id=7)
    db = _Db(results=[notification, None, 5708953214])
    _patch_session(monkeypatch, db)

    response = await android_rate_request.handle_android_rate_request_click_redirect(42)

    assert response.status_code == 302
    assert "sent_notification_id=42" in response.headers["location"]
    assert len(db.added) == 1
    click = db.added[0]
    assert click.sent_notification_id == 42
    assert click.user_id == 7
    assert click.telegram_id == 5708953214
    db.commit.assert_awaited_once()
    db.rollback.assert_not_awaited()


async def test_android_rate_request_redirect_skips_duplicate_click(monkeypatch):
    notification = SimpleNamespace(id=43, user_id=7)
    db = _Db(results=[notification, 1])
    _patch_session(monkeypatch, db)

    response = await android_rate_request.handle_android_rate_request_click_redirect(43)

    assert response.status_code == 302
    assert "sent_notification_id=43" in response.headers["location"]
    assert db.added == []
    db.commit.assert_not_awaited()
    db.rollback.assert_not_awaited()


async def test_android_rate_request_redirect_works_without_notification(monkeypatch):
    db = _Db(results=[None])
    _patch_session(monkeypatch, db)

    response = await android_rate_request.handle_android_rate_request_click_redirect(44)

    assert response.status_code == 302
    assert "sent_notification_id=44" in response.headers["location"]
    assert db.added == []
    db.commit.assert_not_awaited()


async def test_android_rate_request_redirect_survives_db_error(monkeypatch):
    db = _Db(error=RuntimeError("db down"))
    _patch_session(monkeypatch, db)

    response = await android_rate_request.handle_android_rate_request_click_redirect(45)

    assert response.status_code == 302
    assert "sent_notification_id=45" in response.headers["location"]
    db.rollback.assert_awaited_once()
