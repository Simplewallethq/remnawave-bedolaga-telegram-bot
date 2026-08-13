"""Защита от отката подписки данными устаревшего слепка панели.

Регресс: полный синк панель→БД читает панель несколько минут, а потом пишет
локальные поля из этого снимка. Покупка, прошедшая в этом окне, откатывалась
к досинковым (триальным) значениям: лимит трафика, лимит устройств, дата
окончания — вплоть до end_date < start_date.
"""

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
import sys
from unittest.mock import AsyncMock

from zoneinfo import ZoneInfo

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.database.models import SubscriptionStatus
from app.services.remnawave_service import RemnaWaveService


def _create_service() -> RemnaWaveService:
    service = RemnaWaveService.__new__(RemnaWaveService)
    service._panel_timezone = ZoneInfo("UTC")
    service._utc_timezone = ZoneInfo("UTC")
    return service


def _make_subscription(**overrides):
    now = datetime.utcnow()
    subscription = SimpleNamespace(
        id=1,
        user_id=1,
        status=SubscriptionStatus.ACTIVE.value,
        is_trial=False,
        plan_id=4,
        start_date=now,
        end_date=now + timedelta(days=90),
        traffic_limit_gb=0,
        traffic_used_gb=0.0,
        device_limit=10,
        connected_squads=["squad-1"],
        remnawave_short_uuid="short-uuid",
        subscription_url="https://panel/sub/short-uuid",
        subscription_crypto_link="happ://crypt",
        updated_at=now,
    )
    for key, value in overrides.items():
        setattr(subscription, key, value)
    return subscription


def _make_user(subscription):
    return SimpleNamespace(
        id=subscription.user_id,
        telegram_id=911443828,
        has_connected_to_vpn=True,
        subscription=subscription,
    )


def _make_panel_user(*, expire_at: datetime, traffic_limit_gb: int, device_limit: int):
    return {
        "telegramId": 911443828,
        "status": "ACTIVE",
        "expireAt": expire_at.isoformat() + "Z",
        "trafficLimitBytes": traffic_limit_gb * (1024**3),
        "usedTrafficBytes": 0,
        "hwidDeviceLimit": device_limit,
        "shortUuid": "short-uuid",
        "subscriptionUrl": "https://panel/sub/short-uuid",
        "subscriptionCryptoLink": "happ://crypt",
        "activeInternalSquads": ["squad-1"],
    }


def _make_db(fresh_updated_at: datetime) -> AsyncMock:
    db = AsyncMock()
    db.scalar = AsyncMock(return_value=fresh_updated_at)
    return db


async def test_skips_subscription_changed_after_panel_snapshot():
    """Подписку купили во время обхода панели — снимок применять нельзя."""
    service = _create_service()

    snapshot_taken_at = datetime.utcnow() - timedelta(minutes=5)
    subscription = _make_subscription()
    user = _make_user(subscription)
    # Панель отдаёт досинковое (триальное) состояние.
    panel_user = _make_panel_user(
        expire_at=subscription.start_date - timedelta(days=1),
        traffic_limit_gb=10,
        device_limit=1,
    )

    applied = await service._update_subscription_from_panel_data(
        _make_db(datetime.utcnow()),
        user,
        panel_user,
        snapshot_taken_at=snapshot_taken_at,
    )

    assert applied is False
    assert subscription.traffic_limit_gb == 0
    assert subscription.device_limit == 10
    assert subscription.end_date > subscription.start_date
    assert subscription.status == SubscriptionStatus.ACTIVE.value


async def test_plan_managed_subscription_keeps_bot_limits():
    """Лимиты и срок тарифной подписки задаёт бот, а не панель."""
    service = _create_service()

    subscription = _make_subscription()
    original_end_date = subscription.end_date
    user = _make_user(subscription)
    panel_user = _make_panel_user(
        expire_at=datetime.utcnow() - timedelta(days=1),
        traffic_limit_gb=10,
        device_limit=1,
    )

    # Слепок свежий: строку никто не трогал, но тарифные поля всё равно ведомые.
    applied = await service._update_subscription_from_panel_data(
        _make_db(datetime.utcnow() - timedelta(hours=1)),
        user,
        panel_user,
        snapshot_taken_at=datetime.utcnow() - timedelta(minutes=1),
    )

    assert applied is True
    assert subscription.traffic_limit_gb == 0
    assert subscription.device_limit == 10
    assert subscription.end_date == original_end_date


async def test_trial_subscription_still_imports_panel_limits():
    """Для нетарифных подписок панель по-прежнему источник истины."""
    service = _create_service()

    subscription = _make_subscription(
        is_trial=True,
        plan_id=None,
        traffic_limit_gb=0,
        device_limit=10,
    )
    user = _make_user(subscription)
    panel_expire = datetime.utcnow() + timedelta(days=3)
    panel_user = _make_panel_user(
        expire_at=panel_expire,
        traffic_limit_gb=10,
        device_limit=1,
    )

    applied = await service._update_subscription_from_panel_data(
        _make_db(datetime.utcnow() - timedelta(hours=1)),
        user,
        panel_user,
        snapshot_taken_at=datetime.utcnow() - timedelta(minutes=1),
    )

    assert applied is True
    assert subscription.traffic_limit_gb == 10
    assert subscription.device_limit == 1
    assert abs((subscription.end_date - panel_expire).total_seconds()) < 1


async def test_never_writes_end_date_before_start_date():
    """Дата окончания раньше начала — невозможное состояние, не применяем."""
    service = _create_service()

    subscription = _make_subscription(is_trial=True, plan_id=None)
    original_end_date = subscription.end_date
    user = _make_user(subscription)
    panel_user = _make_panel_user(
        expire_at=subscription.start_date - timedelta(days=1),
        traffic_limit_gb=10,
        device_limit=1,
    )

    applied = await service._update_subscription_from_panel_data(
        _make_db(datetime.utcnow() - timedelta(hours=1)),
        user,
        panel_user,
        snapshot_taken_at=datetime.utcnow() - timedelta(minutes=1),
    )

    assert applied is True
    assert subscription.end_date == original_end_date
    assert subscription.status == SubscriptionStatus.ACTIVE.value


async def test_staleness_check_disabled_without_snapshot_time():
    """Без переданного времени слепка поведение прежнее (обратная совместимость)."""
    service = _create_service()

    subscription = _make_subscription(is_trial=True, plan_id=None)
    user = _make_user(subscription)
    panel_expire = datetime.utcnow() + timedelta(days=5)
    panel_user = _make_panel_user(
        expire_at=panel_expire,
        traffic_limit_gb=50,
        device_limit=2,
    )
    db = _make_db(datetime.utcnow())

    applied = await service._update_subscription_from_panel_data(db, user, panel_user)

    assert applied is True
    db.scalar.assert_not_awaited()
    assert subscription.traffic_limit_gb == 50
    assert subscription.device_limit == 2
