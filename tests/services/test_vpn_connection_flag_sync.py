import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

from app.external.remnawave_api import RemnaWaveUser, TrafficLimitStrategy, UserStatus, UserTraffic
from app.services.monitoring_service import MonitoringService
from app.services.remnawave_service import RemnaWaveService
from app.services.subscription_service import SubscriptionService


class _ExecuteResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _UpdateResult:
    rowcount = 1


class _NotificationExecuteResult:
    def scalar_one_or_none(self):
        return None


def _api_context(api):
    @asynccontextmanager
    async def context():
        yield api

    return context()


def _make_remnawave_user(
    *,
    used_traffic_bytes: int = 0,
    lifetime_used_traffic_bytes: int = 0,
    first_connected_at=None,
) -> RemnaWaveUser:
    now = datetime(2026, 1, 1)
    return RemnaWaveUser(
        uuid="rw-uuid",
        short_uuid="short",
        username="user",
        status=UserStatus.ACTIVE,
        traffic_limit_bytes=0,
        traffic_limit_strategy=TrafficLimitStrategy.NO_RESET,
        expire_at=now + timedelta(days=30),
        telegram_id=123,
        email=None,
        hwid_device_limit=None,
        description=None,
        tag=None,
        subscription_url="",
        active_internal_squads=[],
        created_at=now,
        updated_at=now,
        user_traffic=UserTraffic(
            used_traffic_bytes=used_traffic_bytes,
            lifetime_used_traffic_bytes=lifetime_used_traffic_bytes,
            first_connected_at=first_connected_at,
        ),
    )


async def test_sync_subscription_usage_sets_vpn_flag_from_used_traffic(monkeypatch):
    service = SubscriptionService.__new__(SubscriptionService)
    user = SimpleNamespace(id=1, telegram_id=123, remnawave_uuid="rw-uuid", has_connected_to_vpn=False)
    subscription = SimpleNamespace(id=10, user_id=1, traffic_used_gb=0.0)
    api = SimpleNamespace(get_user_by_uuid=AsyncMock(return_value=_make_remnawave_user(used_traffic_bytes=1024)))
    db = AsyncMock()

    monkeypatch.setattr("app.services.subscription_service.get_user_by_id", AsyncMock(return_value=user))
    service.get_api_client = lambda: _api_context(api)

    assert await service.sync_subscription_usage(db, subscription) is True
    assert user.has_connected_to_vpn is True
    assert subscription.traffic_used_gb > 0
    db.commit.assert_awaited_once()


async def test_sync_subscription_usage_sets_vpn_flag_from_lifetime_traffic(monkeypatch):
    service = SubscriptionService.__new__(SubscriptionService)
    user = SimpleNamespace(id=1, telegram_id=123, remnawave_uuid="rw-uuid", has_connected_to_vpn=False)
    subscription = SimpleNamespace(id=10, user_id=1, traffic_used_gb=0.0)
    api = SimpleNamespace(get_user_by_uuid=AsyncMock(return_value=_make_remnawave_user(lifetime_used_traffic_bytes=1024)))
    db = AsyncMock()

    monkeypatch.setattr("app.services.subscription_service.get_user_by_id", AsyncMock(return_value=user))
    service.get_api_client = lambda: _api_context(api)

    assert await service.sync_subscription_usage(db, subscription) is True
    assert user.has_connected_to_vpn is True
    db.commit.assert_awaited_once()


async def test_batch_vpn_flag_sync_updates_from_panel_pages():
    connected = _make_remnawave_user(used_traffic_bytes=1024)
    connected.uuid = "connected-uuid"
    not_connected = _make_remnawave_user()
    not_connected.uuid = "not-connected-uuid"

    api = SimpleNamespace(
        get_all_users=AsyncMock(
            return_value={"users": [connected, not_connected], "total": 2}
        )
    )
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[_ExecuteResult([(10, "connected-uuid")]), _UpdateResult()])

    service = RemnaWaveService.__new__(RemnaWaveService)
    service._config_error = None
    service.api = object()
    service.get_api_client = lambda: _api_context(api)

    stats = await service.sync_vpn_connection_flags_from_panel(db, page_size=1000)

    assert stats["local_candidates"] == 1
    assert stats["panel_users_scanned"] == 2
    assert stats["matched"] == 1
    assert stats["updated"] == 1
    api.get_all_users.assert_awaited_once_with(start=0, size=1000, enrich_happ_links=False)
    db.commit.assert_awaited_once()


async def test_batch_vpn_flag_sync_stops_when_all_candidates_found():
    first = _make_remnawave_user(used_traffic_bytes=1024)
    first.uuid = "first-uuid"
    api = SimpleNamespace(
        get_all_users=AsyncMock(
            return_value={"users": [first], "total": 10000}
        )
    )
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[_ExecuteResult([(10, "first-uuid")]), _UpdateResult()])

    service = RemnaWaveService.__new__(RemnaWaveService)
    service._config_error = None
    service.api = object()
    service.get_api_client = lambda: _api_context(api)

    stats = await service.sync_vpn_connection_flags_from_panel(db, page_size=1000)

    assert stats["pages"] == 1
    assert stats["updated"] == 1
    api.get_all_users.assert_awaited_once()


async def test_batch_vpn_flag_sync_caps_remnawave_page_size():
    connected = _make_remnawave_user(used_traffic_bytes=1024)
    connected.uuid = "connected-uuid"
    api = SimpleNamespace(
        get_all_users=AsyncMock(return_value={"users": [connected], "total": 1})
    )
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[_ExecuteResult([(10, "connected-uuid")]), _UpdateResult()])

    service = RemnaWaveService.__new__(RemnaWaveService)
    service._config_error = None
    service.api = object()
    service.get_api_client = lambda: _api_context(api)

    await service.sync_vpn_connection_flags_from_panel(db, page_size=5000)

    api.get_all_users.assert_awaited_once_with(start=0, size=1000, enrich_happ_links=False)


async def test_monitoring_remnawave_sync_uses_batch_sync_without_exact_hour(monkeypatch):
    class _FixedDatetime:
        @staticmethod
        def utcnow():
            return datetime(2026, 7, 27, 9, 13, 0)

        min = datetime.min

    db = AsyncMock()
    batch_sync = AsyncMock(return_value={"updated": 1})

    service = MonitoringService.__new__(MonitoringService)
    service.subscription_service = SimpleNamespace(is_configured=True)
    service._last_remnawave_sync_at = None
    service._remnawave_sync_lock = __import__("asyncio").Lock()
    service._log_monitoring_event = AsyncMock()

    monkeypatch.setattr("app.services.monitoring_service.datetime", _FixedDatetime)
    monkeypatch.setattr(
        "app.services.monitoring_service.RemnaWaveService",
        lambda: SimpleNamespace(sync_vpn_connection_flags_from_panel=batch_sync),
    )

    await service._sync_with_remnawave(db)

    batch_sync.assert_awaited_once_with(db)
    service._log_monitoring_event.assert_awaited_once()
    assert service._last_remnawave_sync_at == datetime(2026, 7, 27, 9, 13, 0)


async def test_monitoring_remnawave_sync_skips_when_previous_batch_is_running(monkeypatch):
    lock = asyncio.Lock()
    await lock.acquire()

    service = MonitoringService.__new__(MonitoringService)
    service.subscription_service = SimpleNamespace(is_configured=True)
    service._last_remnawave_sync_at = None
    service._remnawave_sync_lock = lock
    service._log_monitoring_event = AsyncMock()

    monkeypatch.setattr(
        "app.services.monitoring_service.RemnaWaveService",
        lambda: (_ for _ in ()).throw(AssertionError("batch sync should be skipped")),
    )

    try:
        await service._sync_with_remnawave(AsyncMock())
    finally:
        lock.release()

    service._log_monitoring_event.assert_not_awaited()


async def test_trial_inactivity_notifications_skip_connected_user(monkeypatch):
    now = datetime.utcnow()
    user = SimpleNamespace(id=1, telegram_id=123, language="ru", has_connected_to_vpn=True)
    subscription = SimpleNamespace(
        id=10,
        user=user,
        start_date=now - timedelta(hours=2),
        end_date=now + timedelta(days=1),
        traffic_used_gb=0.0,
    )
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_ExecuteResult([subscription]))

    service = MonitoringService.__new__(MonitoringService)
    service.bot = object()
    service._send_trial_inactive_notification = AsyncMock()
    service._log_monitoring_event = AsyncMock()

    await service._check_trial_inactivity_notifications(db)

    service._send_trial_inactive_notification.assert_not_awaited()


async def test_trial_inactivity_1h_uses_flag_not_traffic(monkeypatch):
    now = datetime.utcnow()
    user = SimpleNamespace(id=1, telegram_id=123, language="ru", has_connected_to_vpn=False)
    subscription = SimpleNamespace(
        id=10,
        user=user,
        start_date=now - timedelta(hours=2),
        end_date=now + timedelta(days=1),
        traffic_used_gb=42.0,
    )
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[_ExecuteResult([subscription]), _NotificationExecuteResult()])
    db.add = MagicMock()

    service = MonitoringService.__new__(MonitoringService)
    service.bot = object()
    service._send_trial_inactive_notification = AsyncMock(return_value=True)
    service._log_monitoring_event = AsyncMock()

    await service._check_trial_inactivity_notifications(db)

    service._send_trial_inactive_notification.assert_awaited_once_with(user, subscription, 1)


async def test_trial_inactivity_24h_uses_flag_not_traffic(monkeypatch):
    now = datetime.utcnow()
    user = SimpleNamespace(id=1, telegram_id=123, language="ru", has_connected_to_vpn=False)
    subscription = SimpleNamespace(
        id=10,
        user=user,
        start_date=now - timedelta(hours=25),
        end_date=now + timedelta(days=1),
        traffic_used_gb=42.0,
    )
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[_ExecuteResult([subscription]), _NotificationExecuteResult()])
    db.add = MagicMock()

    service = MonitoringService.__new__(MonitoringService)
    service.bot = object()
    service._send_trial_inactive_notification = AsyncMock(return_value=True)
    service._log_monitoring_event = AsyncMock()

    await service._check_trial_inactivity_notifications(db)

    service._send_trial_inactive_notification.assert_awaited_once_with(user, subscription, 24)


async def test_trial_inactive_sender_uses_hardcoded_1h_message_and_buttons():
    user = SimpleNamespace(id=1, telegram_id=123, language="en")
    subscription = SimpleNamespace(end_date=datetime.utcnow() + timedelta(days=1))

    service = MonitoringService.__new__(MonitoringService)
    service._send_message_with_logo = AsyncMock()

    assert await service._send_trial_inactive_notification(user, subscription, 1) is True

    call_kwargs = service._send_message_with_logo.await_args.kwargs
    keyboard = call_kwargs["reply_markup"]
    buttons = [row[0] for row in keyboard.inline_keyboard]

    assert call_kwargs["text"] == (
        "👋 <b>Застрял на подключении?</b>\n\n"
        "Доступ уже активен — покажем, как подключиться за минуту."
    )
    assert len(keyboard.inline_keyboard) == 2
    assert [(button.text, button.callback_data) for button in buttons] == [
        ("📲 Подключиться", "subscription_connect"),
        ("🆘 Поддержка", "menu_support"),
    ]
    assert all(button.text != "📱 Моя подписка" for button in buttons)


async def test_trial_inactive_sender_uses_hardcoded_24h_message_and_buttons():
    user = SimpleNamespace(id=1, telegram_id=123, language="en")
    subscription = SimpleNamespace(end_date=datetime.utcnow() + timedelta(days=1))

    service = MonitoringService.__new__(MonitoringService)
    service._send_message_with_logo = AsyncMock()

    assert await service._send_trial_inactive_notification(user, subscription, 24) is True

    call_kwargs = service._send_message_with_logo.await_args.kwargs
    keyboard = call_kwargs["reply_markup"]
    buttons = [row[0] for row in keyboard.inline_keyboard]

    assert call_kwargs["text"] == (
        "⏳ <b>Твой тест уходит впустую</b>\n\n"
        "Сутки прошли, а VPN так и не подключён. Давай исправим —\n"
        "это пара минут."
    )
    assert len(keyboard.inline_keyboard) == 2
    assert [(button.text, button.callback_data) for button in buttons] == [
        ("📲 Подключиться", "subscription_connect"),
        ("🆘 Поддержка", "menu_support"),
    ]
    assert all(button.text != "📱 Моя подписка" for button in buttons)


async def test_panel_subscription_sync_sets_vpn_flag_from_panel_traffic():
    service = RemnaWaveService.__new__(RemnaWaveService)
    service._panel_timezone = ZoneInfo("UTC")
    service._utc_timezone = ZoneInfo("UTC")

    subscription = SimpleNamespace(
        status="active",
        end_date=datetime(2026, 7, 30),
        traffic_used_gb=0.0,
        traffic_limit_gb=0,
        device_limit=1,
        remnawave_short_uuid="old-short",
        subscription_url="",
        subscription_crypto_link="",
        connected_squads=[],
    )
    user = SimpleNamespace(id=1, telegram_id=123, has_connected_to_vpn=False, subscription=subscription)
    panel_user = {
        "status": "ACTIVE",
        "expireAt": "2026-08-01T00:00:00Z",
        "trafficLimitBytes": 0,
        "userTraffic": {"usedTrafficBytes": 1024**3, "lifetimeUsedTrafficBytes": 1024**3},
        "shortUuid": "new-short",
        "subscriptionUrl": "https://example.test/sub",
        "activeInternalSquads": [],
    }

    await service._update_subscription_from_panel_data(AsyncMock(), user, panel_user)

    assert user.has_connected_to_vpn is True
    assert subscription.traffic_used_gb > 0
