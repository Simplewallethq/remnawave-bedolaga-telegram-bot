from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from app.services import android_rate_request_service as service_module
from app.services.android_rate_request_service import (
    ANDROID_RATE_REQUEST_BATCH_SIZE,
    ANDROID_RATE_REQUEST_DEBUG_TELEGRAM_ID,
    ANDROID_RATE_REQUEST_NOTIFICATION_TYPE,
    ANDROID_RATE_REQUEST_TRAFFIC_THRESHOLD_BYTES,
    AndroidRateRequestService,
    build_android_rate_request_review_url,
    build_android_rate_request_tracking_url,
)


MOSCOW_TZ = ZoneInfo("Europe/Moscow")
TELEGRAM_ID = 5708953214


class _RowsResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _ScalarResult:
    def __init__(self, value=None, values=None):
        self._value = value
        self._values = values or []

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return self

    def unique(self):
        return self

    def all(self):
        return self._values


class _TrafficDb:
    def __init__(self, rows):
        self.rows = rows

    async def execute(self, _statement):
        return _RowsResult(self.rows)


class _ProcessDb:
    def __init__(self):
        self.added = []
        self.commit = AsyncMock()
        self.rollback = AsyncMock()

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        value = self.added[-1]
        if getattr(value, "id", None) is None:
            value.id = len(self.added)


def _service_at(value: datetime) -> AndroidRateRequestService:
    return AndroidRateRequestService(now_provider=lambda: value)


def test_send_window_allows_20_to_22_moscow():
    service = AndroidRateRequestService()

    assert service._is_send_window(datetime(2024, 1, 1, 20, 0, tzinfo=MOSCOW_TZ))
    assert service._is_send_window(datetime(2024, 1, 1, 21, 59, tzinfo=MOSCOW_TZ))
    assert not service._is_send_window(datetime(2024, 1, 1, 19, 59, tzinfo=MOSCOW_TZ))
    assert not service._is_send_window(datetime(2024, 1, 1, 22, 0, tzinfo=MOSCOW_TZ))


async def test_has_required_traffic_requires_three_completed_days():
    now = datetime(2024, 1, 10, 21, 0, tzinfo=MOSCOW_TZ)
    rows = [
        (date(2024, 1, 9), ANDROID_RATE_REQUEST_TRAFFIC_THRESHOLD_BYTES),
        (date(2024, 1, 8), ANDROID_RATE_REQUEST_TRAFFIC_THRESHOLD_BYTES + 1),
        (date(2024, 1, 7), ANDROID_RATE_REQUEST_TRAFFIC_THRESHOLD_BYTES),
    ]
    service = _service_at(now)

    assert await service._has_required_traffic(_TrafficDb(rows), 1, now)


async def test_has_required_traffic_rejects_missing_or_low_day():
    now = datetime(2024, 1, 10, 21, 0, tzinfo=MOSCOW_TZ)
    rows = [
        (date(2024, 1, 9), ANDROID_RATE_REQUEST_TRAFFIC_THRESHOLD_BYTES),
        (date(2024, 1, 8), ANDROID_RATE_REQUEST_TRAFFIC_THRESHOLD_BYTES - 1),
    ]
    service = _service_at(now)

    assert not await service._has_required_traffic(_TrafficDb(rows), 1, now)


async def test_process_due_requests_sends_and_records(monkeypatch):
    now = datetime(2024, 1, 10, 21, 0, tzinfo=MOSCOW_TZ)
    service = _service_at(now)
    db = _ProcessDb()
    bot = SimpleNamespace(send_message=AsyncMock())
    user = SimpleNamespace(id=1, telegram_id=TELEGRAM_ID)
    subscription = SimpleNamespace(id=2, user=user)

    service._get_last_run_date = AsyncMock(return_value=None)
    service._get_candidate_subscriptions = AsyncMock(side_effect=[[subscription], []])
    upsert_system_setting = AsyncMock()
    monkeypatch.setattr(service_module.settings, "WEBHOOK_URL", "https://example.test")
    monkeypatch.setattr(service_module, "upsert_system_setting", upsert_system_setting)

    result = await service.process_due_requests(db, bot)

    assert result["sent"] == 1
    assert result["candidates"] == 1
    bot.send_message.assert_awaited_once()
    assert service._get_candidate_subscriptions.await_args_list[0].kwargs == {
        "limit": ANDROID_RATE_REQUEST_BATCH_SIZE,
        "after_user_id": 0,
    }
    assert service._get_candidate_subscriptions.await_args_list[1].kwargs == {
        "limit": ANDROID_RATE_REQUEST_BATCH_SIZE,
        "after_user_id": user.id,
    }
    notification = db.added[0]
    assert notification.user_id == user.id
    assert notification.subscription_id == subscription.id
    assert notification.notification_type == ANDROID_RATE_REQUEST_NOTIFICATION_TYPE
    reply_markup = bot.send_message.await_args.kwargs["reply_markup"]
    button = reply_markup.inline_keyboard[0][0]
    assert button.callback_data is None
    assert button.url == (
        f"https://example.test/android-rate-request/click?sent_notification_id={notification.id}"
    )
    assert "<b>Спасибо, что пользуешься Leto VPN</b>" in bot.send_message.await_args.kwargs["text"]
    upsert_system_setting.assert_awaited_once()
    assert db.commit.await_count == 2


async def test_process_due_requests_respects_daily_guard(monkeypatch):
    now = datetime(2024, 1, 10, 21, 0, tzinfo=MOSCOW_TZ)
    service = _service_at(now)
    db = SimpleNamespace(commit=AsyncMock())
    bot = SimpleNamespace(send_message=AsyncMock())

    service._get_last_run_date = AsyncMock(return_value="2024-01-10")
    service._get_candidate_subscriptions = AsyncMock(return_value=[])
    monkeypatch.setattr(service_module, "upsert_system_setting", AsyncMock())

    result = await service.process_due_requests(db, bot)

    assert result == {"skipped": True, "reason": "already_processed_today", "date": "2024-01-10"}
    service._get_candidate_subscriptions.assert_not_awaited()
    bot.send_message.assert_not_awaited()
    db.commit.assert_not_awaited()


async def test_debug_mode_ignores_send_window_and_daily_guard(monkeypatch):
    now = datetime(2024, 1, 10, 10, 0, tzinfo=MOSCOW_TZ)
    service = _service_at(now)
    db = _ProcessDb()
    bot = SimpleNamespace(send_message=AsyncMock())
    user = SimpleNamespace(id=1, telegram_id=TELEGRAM_ID)
    subscription = SimpleNamespace(id=2, user=user)

    service._get_last_run_date = AsyncMock(return_value="2024-01-10")
    service._get_candidate_subscriptions = AsyncMock(side_effect=[[subscription], []])
    monkeypatch.setattr(service_module, "IS_DEBUG", True)
    monkeypatch.setattr(service_module.settings, "WEBHOOK_URL", "https://example.test")
    monkeypatch.setattr(service_module, "upsert_system_setting", AsyncMock())

    result = await service.process_due_requests(db, bot)

    assert result["sent"] == 1
    assert result["skipped"] is False
    bot.send_message.assert_awaited_once()


async def test_process_due_requests_processes_all_batches(monkeypatch):
    now = datetime(2024, 1, 10, 21, 0, tzinfo=MOSCOW_TZ)
    service = _service_at(now)
    db = _ProcessDb()
    bot = SimpleNamespace(send_message=AsyncMock())
    first_user = SimpleNamespace(id=1, telegram_id=TELEGRAM_ID)
    second_user = SimpleNamespace(id=2, telegram_id=TELEGRAM_ID)
    first_subscription = SimpleNamespace(id=2, user=first_user)
    second_subscription = SimpleNamespace(id=3, user=second_user)

    service._get_last_run_date = AsyncMock(return_value=None)
    service._get_candidate_subscriptions = AsyncMock(side_effect=[
        [first_subscription],
        [second_subscription],
        [],
    ])
    monkeypatch.setattr(service_module, "upsert_system_setting", AsyncMock())

    result = await service.process_due_requests(db, bot)

    assert result["sent"] == 2
    assert result["candidates"] == 2
    assert bot.send_message.await_count == 2
    assert len(db.added) == 2
    assert [call.kwargs["after_user_id"] for call in service._get_candidate_subscriptions.await_args_list] == [
        0,
        first_user.id,
        second_user.id,
    ]


async def test_process_due_requests_does_not_record_unreachable(monkeypatch):
    now = datetime(2024, 1, 10, 21, 0, tzinfo=MOSCOW_TZ)
    service = _service_at(now)
    db = _ProcessDb()
    bot = SimpleNamespace(send_message=AsyncMock())
    user = SimpleNamespace(id=1, telegram_id=TELEGRAM_ID)
    subscription = SimpleNamespace(id=2, user=user)

    service._get_last_run_date = AsyncMock(return_value=None)
    service._get_candidate_subscriptions = AsyncMock(side_effect=[[subscription], []])
    service._send_rate_request = AsyncMock(return_value="unreachable")
    monkeypatch.setattr(service_module, "upsert_system_setting", AsyncMock())

    result = await service.process_due_requests(db, bot)

    assert result["unreachable"] == 1
    assert result["sent"] == 0
    db.rollback.assert_awaited_once()


async def test_process_due_requests_logs_record_error_and_continues(monkeypatch):
    now = datetime(2024, 1, 10, 21, 0, tzinfo=MOSCOW_TZ)
    service = _service_at(now)
    db = _ProcessDb()
    db.commit.side_effect = [RuntimeError("db down"), None]
    bot = SimpleNamespace(send_message=AsyncMock())
    user = SimpleNamespace(id=1, telegram_id=TELEGRAM_ID)
    subscription = SimpleNamespace(id=2, user=user)

    service._get_last_run_date = AsyncMock(return_value=None)
    service._get_candidate_subscriptions = AsyncMock(side_effect=[[subscription], []])
    monkeypatch.setattr(service_module, "upsert_system_setting", AsyncMock())

    result = await service.process_due_requests(db, bot)

    assert result["sent"] == 0
    assert result["failed"] == 1
    db.rollback.assert_awaited_once()


def test_build_android_rate_request_review_url_adds_notification_id():
    url = build_android_rate_request_review_url(123)

    assert "id=com.leto.split" in url
    assert "utm_source=letovpnbot" in url
    assert "sent_notification_id=123" in url


def test_build_android_rate_request_tracking_url_uses_base_url():
    url = build_android_rate_request_tracking_url(123, base_url="https://bot.example/")

    assert url == "https://bot.example/android-rate-request/click?sent_notification_id=123"


async def test_cooldown_uses_latest_sent_notification(monkeypatch):
    now = datetime(2024, 1, 31, 21, 0, tzinfo=MOSCOW_TZ)
    service = _service_at(now)
    monkeypatch.setattr(
        service_module,
        "get_latest_notification_sent_at",
        AsyncMock(return_value=datetime(2024, 1, 2, 18, 0)),
    )

    assert await service._is_in_cooldown(SimpleNamespace(), 1, now)

    monkeypatch.setattr(
        service_module,
        "get_latest_notification_sent_at",
        AsyncMock(return_value=datetime(2024, 1, 1, 17, 59)),
    )
    assert not await service._is_in_cooldown(SimpleNamespace(), 1, now)


async def test_candidate_query_requires_android_app_usage():
    now = datetime(2024, 1, 10, 21, 0, tzinfo=MOSCOW_TZ)
    service = _service_at(now)
    captured = {}

    class Db:
        async def execute(self, statement):
            captured["sql"] = str(statement.compile(compile_kwargs={"literal_binds": True}))
            return _ScalarResult(values=[])

    await service._get_candidate_subscriptions(Db(), now, limit=123, after_user_id=456)

    assert str(TELEGRAM_ID) not in captured["sql"]
    assert "users.has_used_mobile_app = true" in captured["sql"].lower()
    assert "subscriptions.is_trial = false" in captured["sql"].lower()
    assert "user_daily_traffic_usage" in captured["sql"]
    assert "sent_notifications" in captured["sql"]
    assert "users.id > 456" in captured["sql"]
    assert "LIMIT 123" in captured["sql"]
    assert "OFFSET" not in captured["sql"]


async def test_debug_candidate_query_targets_only_debug_user(monkeypatch):
    now = datetime(2024, 1, 10, 10, 0, tzinfo=MOSCOW_TZ)
    service = _service_at(now)
    captured = {}
    monkeypatch.setattr(service_module, "IS_DEBUG", True)

    class Db:
        async def execute(self, statement):
            captured["sql"] = str(statement.compile(compile_kwargs={"literal_binds": True}))
            return _ScalarResult(values=[])

    await service._get_candidate_subscriptions(Db(), now, limit=123, after_user_id=456)

    sql = captured["sql"].lower()
    assert f"users.telegram_id = {ANDROID_RATE_REQUEST_DEBUG_TELEGRAM_ID}" in sql
    assert "users.id > 456" in sql
    assert "users.has_used_mobile_app" not in sql
    assert "user_daily_traffic_usage" not in sql
    assert "sent_notifications" not in sql
    assert "subscriptions.is_trial = false" in sql
    assert "limit 1" in sql
