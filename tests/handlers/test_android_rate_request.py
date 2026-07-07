from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.handlers.android_rate_request import handle_android_rate_request_click
from app.services.android_rate_request_service import (
    ANDROID_RATE_REQUEST_CLICK_CALLBACK_PREFIX,
)


class _ScalarResult:
    def __init__(self, value=None):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _Db:
    def __init__(self):
        self.added = []
        self.existing_notification_ids = set()
        self.commit = AsyncMock()
        self.rollback = AsyncMock()

    async def execute(self, _statement):
        notification_id = getattr(self, "current_notification_id", None)
        if notification_id in self.existing_notification_ids:
            return _ScalarResult(1)
        return _ScalarResult()

    def add(self, value):
        self.added.append(value)
        self.existing_notification_ids.add(value.sent_notification_id)


def _callback(notification_id: int):
    return SimpleNamespace(
        id="callback-id",
        data=f"{ANDROID_RATE_REQUEST_CLICK_CALLBACK_PREFIX}:{notification_id}",
        from_user=SimpleNamespace(id=5708953214),
        message=SimpleNamespace(message_id=123, answer=AsyncMock()),
        bot=SimpleNamespace(send_message=AsyncMock()),
        answer=AsyncMock(),
    )


async def test_android_rate_request_click_is_recorded_and_redirected():
    db = _Db()
    db.current_notification_id = 42
    callback = _callback(42)
    db_user = SimpleNamespace(id=7)

    await handle_android_rate_request_click(callback, db, db_user)

    assert len(db.added) == 1
    click = db.added[0]
    assert click.sent_notification_id == 42
    assert click.user_id == 7
    assert click.telegram_id == 5708953214
    assert click.message_id == 123
    assert click.callback_query_id == "callback-id"
    assert "sent_notification_id=42" in click.review_url
    db.commit.assert_awaited_once()
    db.rollback.assert_not_awaited()
    callback.answer.assert_awaited_once()
    assert "sent_notification_id=42" in callback.answer.await_args.kwargs["url"]


async def test_android_rate_request_click_redirects_when_db_write_fails():
    db = _Db()
    db.current_notification_id = 43
    db.commit.side_effect = RuntimeError("db down")
    callback = _callback(43)

    await handle_android_rate_request_click(callback, db, SimpleNamespace(id=7))

    db.rollback.assert_awaited_once()
    callback.answer.assert_awaited_once()
    assert "sent_notification_id=43" in callback.answer.await_args.kwargs["url"]


async def test_android_rate_request_second_click_is_not_recorded_again():
    db = _Db()
    db.current_notification_id = 44
    first_callback = _callback(44)
    second_callback = _callback(44)

    await handle_android_rate_request_click(first_callback, db, SimpleNamespace(id=7))
    await handle_android_rate_request_click(second_callback, db, SimpleNamespace(id=7))

    assert len(db.added) == 1
    db.commit.assert_awaited_once()
    db.rollback.assert_not_awaited()
    first_callback.answer.assert_awaited_once()
    second_callback.answer.assert_awaited_once()
    assert "sent_notification_id=44" in second_callback.answer.await_args.kwargs["url"]


async def test_android_rate_request_sends_fallback_button_when_redirect_fails():
    db = _Db()
    db.current_notification_id = 45
    callback = _callback(45)
    callback.answer.side_effect = RuntimeError("telegram redirect failed")

    await handle_android_rate_request_click(callback, db, SimpleNamespace(id=7))

    callback.answer.assert_awaited_once()
    callback.message.answer.assert_awaited_once()
    _, kwargs = callback.message.answer.await_args
    assert "Google Play" in kwargs["reply_markup"].inline_keyboard[0][0].text
    assert "sent_notification_id=45" in kwargs["reply_markup"].inline_keyboard[0][0].url
