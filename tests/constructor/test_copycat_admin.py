"""Админка копикетов: метрики, доступ, проверка токена и заведение витрины."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.config import settings
from app.copycat_admin import handlers, service, stats
from app.database.models import CopycatBot, CopycatGroup, Transaction, TransactionType, User

from .conftest import OTHER_TOKEN, PLATFORM_BOT, TOKEN, needs_sqlite, png_bytes

pytestmark = [pytest.mark.anyio]

# 15:00 UTC = 18:00 МСК: «сегодня» начинается в 21:00 UTC прошлых суток.
NOW = datetime(2026, 10, 27, 15, 0)
BOT_A, BOT_B = 777000, 888000


@pytest.fixture
def moscow(monkeypatch):
    from app.utils.timezone import get_local_timezone

    monkeypatch.setattr(settings, "TIMEZONE", "Europe/Moscow", raising=False)
    get_local_timezone.cache_clear()
    yield
    get_local_timezone.cache_clear()


def test_bounds_follow_local_midnight(moscow):
    b = stats.bounds(NOW)
    assert b.today == datetime(2026, 10, 26, 21, 0)
    assert b.yesterday == datetime(2026, 10, 25, 21, 0)
    assert b.d7 == NOW - timedelta(days=7)


def test_settings_parse_admin_ids(monkeypatch):
    monkeypatch.setattr(settings, "COPYCAT_ADMIN_USER_IDS", " 6892239395,158287363, x ,127898413", raising=False)
    assert settings.get_copycat_admin_user_ids() == {6892239395, 158287363, 127898413}


@needs_sqlite
async def test_collect_counts_users_revenue_and_payers(db, moscow):
    users = [
        User(telegram_id=1, referral_code="a1", bot_id=BOT_A, created_at=NOW - timedelta(hours=1)),  # сегодня
        User(telegram_id=2, referral_code="a2", bot_id=BOT_A, created_at=datetime(2026, 10, 26, 20, 0)),  # вчера МСК
        User(telegram_id=3, referral_code="a3", bot_id=BOT_A, created_at=NOW - timedelta(days=20)),
        User(telegram_id=4, referral_code="b1", bot_id=BOT_B, created_at=NOW - timedelta(days=60)),
        User(telegram_id=5, referral_code="m1", bot_id=None, created_at=NOW),
    ]
    db.add_all(users)
    await db.flush()

    def deposit(user, rub, when, method="platega"):
        return Transaction(
            user_id=user.id,
            type=TransactionType.DEPOSIT.value,
            amount_kopeks=rub * 100,
            payment_method=method,
            is_completed=True,
            created_at=when,
        )

    db.add_all(
        [
            deposit(users[0], 290, NOW - timedelta(minutes=30)),
            deposit(users[0], 390, NOW - timedelta(days=3)),
            deposit(users[2], 590, NOW - timedelta(days=20)),
            deposit(users[2], 1000, NOW, method="manual"),  # руками админа — не оборот
            deposit(users[3], 100, NOW - timedelta(days=40)),
            deposit(users[4], 5000, NOW),  # основной бот
        ]
    )
    await db.commit()

    metrics = await stats.collect(db, [BOT_A, BOT_B, 999], now=NOW)
    a, b = metrics[BOT_A], metrics[BOT_B]
    assert vars(a.users) == {"today": 1, "yesterday": 1, "d7": 2, "d30": 3, "total": 3}
    assert vars(a.revenue) == {"today": 29000, "yesterday": 0, "d7": 68000, "d30": 127000, "total": 127000}
    assert a.payers == 2 and a.last_registration == NOW - timedelta(hours=1)
    assert vars(b.users)["total"] == 1 and vars(b.revenue)["d30"] == 0 and b.revenue.total == 10000
    assert 999 not in metrics

    summary = stats.total(metrics.values())
    assert summary.users.total == 4 and summary.revenue.total == 137000 and summary.payers == 3


async def test_access_middleware_lets_only_listed_users(monkeypatch):
    from aiogram.types import CallbackQuery

    monkeypatch.setattr(settings, "COPYCAT_ADMIN_USER_IDS", "100,200", raising=False)
    middleware = handlers.AccessMiddleware()
    handler = AsyncMock(return_value="ok")

    allowed = SimpleNamespace(from_user=SimpleNamespace(id=200))
    assert await middleware(handler, allowed, {}) == "ok"

    stranger = CallbackQuery.model_construct(
        id="1", from_user=SimpleNamespace(id=300, username="x"), chat_instance="c", data="m"
    )
    answer = AsyncMock()
    object.__setattr__(stranger, "answer", answer)
    assert await middleware(handler, stranger, {}) is None
    handler.assert_awaited_once()
    answer.assert_awaited_once_with("Нет доступа", show_alert=True)


@pytest.fixture
def probe(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "OWNER_BOT_IMAGES_DIR", str(tmp_path / "logos"), raising=False)
    monkeypatch.setattr(settings, "WEBHOOK_URL", "https://hooks.example", raising=False)
    monkeypatch.setattr(service.platform, "is_reserved_bot_id", lambda bot_id: bot_id == PLATFORM_BOT)
    bots: dict[str, tuple] = {}

    async def fake_probe(token):
        return bots.get(token, (None, None))

    monkeypatch.setattr(service, "_probe", fake_probe)

    def add(token, bot_id, username, title="Сатурн | VPN", webhook=None):
        bots[token] = (SimpleNamespace(id=bot_id, username=username, full_name=title), webhook)

    return add


@needs_sqlite
async def test_check_token_rejects_bad_reserved_and_duplicate_bots(db, probe):
    with pytest.raises(service.AddError, match="не работает"):
        await service.check_token(db, TOKEN)

    probe(TOKEN, PLATFORM_BOT, "dostupoffbot")
    with pytest.raises(service.AddError, match="служебный"):
        await service.check_token(db, TOKEN)

    db.add(CopycatBot(token=OTHER_TOKEN, telegram_bot_id=BOT_B, username="old_bot", enabled=False))
    await db.commit()
    with pytest.raises(service.AddError, match="уже заведён"):
        await service.check_token(db, OTHER_TOKEN)
    probe(TOKEN, BOT_B, "old_bot")
    with pytest.raises(service.AddError, match="уже есть: #1 \\(выключена\\)"):
        await service.check_token(db, TOKEN)


@needs_sqlite
async def test_new_bot_lands_in_default_group_with_its_picture(db, probe):
    db.add(CopycatGroup(name="default"))
    await db.commit()
    probe(TOKEN, BOT_A, "saturn_vpn_bot", webhook="https://other.example/hook")

    candidate = await service.check_token(db, TOKEN)
    assert candidate.foreign_webhook == "https://other.example/hook"
    logo = service.store_picture(png_bytes(), candidate.bot_id)
    row = await service.create(db, candidate, logo=logo, image_url="https://i.ibb.co/x.png")

    saved = (await db.execute(select(CopycatBot).where(CopycatBot.id == row.id))).scalar_one()
    group = (await db.execute(select(CopycatGroup))).scalar_one()
    assert (saved.telegram_bot_id, saved.username, saved.title) == (BOT_A, "saturn_vpn_bot", "Сатурн | VPN")
    assert saved.group_id == group.id and saved.enabled and saved.is_copycat
    assert saved.name is None  # имя бренда — из названия в Telegram
    assert saved.logo == logo and saved.image_url == "https://i.ibb.co/x.png"


def test_store_picture_rejects_imgbb_placeholder_and_non_images(probe):
    with pytest.raises(service.AddError, match="заглушку"):
        service.store_picture(b"x" * 1115, BOT_A)
    with pytest.raises(service.AddError, match="не картинка"):
        service.store_picture(b"not an image", BOT_A)


def test_money_and_numbers_are_grouped():
    assert handlers.rub(123456700) == "1 234 567₽"
    assert handlers.num(12345) == "12 345"
