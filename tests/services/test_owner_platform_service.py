"""Платформа владельцев: валидация, занятость бота, статистика, заработок и вывод."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.database.models import (
    Base,
    BotOwner,
    CopycatBot,
    OwnerPayout,
    Transaction,
    TransactionType,
    User,
)
from app.services import owner_platform_service as platform
from app.services import price_scheme_service as schemes
from app.utils.tron import is_trc20_address, mask_address

NOW = datetime(2026, 10, 13, 12, 0)
OWNER_BOT = 5001
OTHER_BOT = 5002
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"


def _aiosqlite_available() -> bool:
    module = sys.modules.get("aiosqlite")
    return module is not None and hasattr(module, "DatabaseError")


needs_sqlite = pytest.mark.skipif(not _aiosqlite_available(), reason="настоящий aiosqlite недоступен")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)()


# ---------------------------------------------------------------- валидация


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("@myvpn_bot", "myvpn_bot"),
        ("MyVpnBot", "MyVpnBot"),
        ("https://t.me/myvpn_bot", "myvpn_bot"),
        ("myvpn", None),
        ("bot", None),
        ("my-vpn_bot", None),
        ("a" * 30 + "bot", None),
    ],
)
def test_bot_username(text, expected):
    assert platform.normalize_bot_username(text) == expected


def test_support_username():
    assert platform.normalize_support_username("@support_1") == "support_1"
    assert platform.normalize_support_username("abc") is None
    assert platform.normalize_support_username("кириллица") is None


def test_trc20_address():
    assert is_trc20_address(USDT_CONTRACT)
    assert not is_trc20_address(USDT_CONTRACT[:-1] + "u")  # битая контрольная сумма
    assert not is_trc20_address("0x" + "a" * 40)
    assert not is_trc20_address("1BoatSLRHtKNngkdXEeobR76b53LETtpyT")
    assert mask_address(USDT_CONTRACT) == "TR7N…Lj6t"


def test_token_shape():
    assert platform.looks_like_token("123456789:AAH" + "x" * 32)
    assert not platform.looks_like_token("not a token")


# ---------------------------------------------------------------- цены


@pytest.fixture
def floor(monkeypatch):
    monkeypatch.setattr(settings, "OWNER_PRICE_FLOOR_SCHEME", "owner-floor", raising=False)
    monkeypatch.setattr(settings, "OWNER_PRICE_MIN_SOLO_RUB", 120, raising=False)
    schemes.set_snapshot_for_tests(
        [], base={"solo": {30: 32000}, "plus": {30: 49000}, "pro": {30: 69000}}
    )
    yield
    schemes.set_snapshot_for_tests([])


def test_price_rules(floor):
    # Пол по умолчанию — цены основного бота.
    assert platform.price_error("solo", 319) == ("OWNER_PRICE_SOLO_MIN", {"min": 320})
    assert platform.price_error("solo", 320) is None
    # Plus ≥ Solo +10%, вверх до целого: 333 × 1.1 = 366.3 → 367; и не ниже пола 490.
    assert platform.price_error("plus", 366, solo=333) == ("OWNER_PRICE_PLUS_STEP", {"min": 367})
    assert platform.price_error("plus", 489, solo=333)[0] == "OWNER_PRICE_FLOOR"
    assert platform.price_error("plus", 600, solo=333) is None
    # Pro ≥ Plus +20%: 600 × 1.2 = 720.
    assert platform.price_error("pro", 719, plus=600) == ("OWNER_PRICE_PRO_STEP", {"min": 720})
    assert platform.price_error("pro", 720, plus=600) is None

    assert platform.parse_price("199") == 199
    assert platform.parse_price("199.5") is None
    assert platform.parse_price("сто") is None


def test_solo_minimum_holds_when_floor_is_lower(monkeypatch):
    schemes.set_snapshot_for_tests([], base={"solo": {30: 9900}})
    try:
        assert platform.price_error("solo", 119) == ("OWNER_PRICE_SOLO_MIN", {"min": 120})
        assert platform.price_error("solo", 120) is None
    finally:
        schemes.set_snapshot_for_tests([])


# ---------------------------------------------------------------- вывод


def test_usdt_math(monkeypatch):
    monkeypatch.setattr(settings, "OWNER_PAYOUT_FEE_USDT", 3.0, raising=False)
    rate = Decimal("107.00")
    # 1000 ₽ / 107 = 9.3457… → вниз 9.34
    assert platform.rub_to_usdt(100000, rate) == Decimal("9.34")
    assert platform.usdt_to_rub_kopeks(Decimal("9.34"), rate) == 99938
    assert platform.parse_usdt("50") == Decimal("50.00")
    assert platform.parse_usdt("12,5") == Decimal("12.50")
    assert platform.parse_usdt("1.234") is None
    assert platform.parse_usdt("abc") is None
    assert platform.payout_amount_error(Decimal("10"), Decimal("9.34")) == "OWNER_PAYOUT_TOO_MUCH"
    assert platform.payout_amount_error(Decimal("3"), Decimal("9.34")) == "OWNER_PAYOUT_BELOW_FEE"
    assert platform.payout_amount_error(Decimal("3.01"), Decimal("9.34")) is None


# ---------------------------------------------------------------- БД


async def _owner_world(db: AsyncSession):
    owner_user = User(telegram_id=1, referral_code="owner", bot_id=None)
    db.add(owner_user)
    await db.flush()
    owner = BotOwner(user_id=owner_user.id, mode="owner")
    db.add(owner)
    db.add_all(
        [
            CopycatBot(token="t1", telegram_bot_id=OWNER_BOT, username="my_bot", enabled=True, owner_user_id=owner_user.id),
            CopycatBot(token="t2", telegram_bot_id=OTHER_BOT, username="gone_bot", enabled=False, owner_user_id=owner_user.id),
        ]
    )
    buyers = [
        User(telegram_id=10, referral_code="b1", bot_id=OWNER_BOT, created_at=NOW - timedelta(days=30)),
        User(telegram_id=11, referral_code="b2", bot_id=OTHER_BOT, created_at=NOW - timedelta(hours=20)),
        User(telegram_id=12, referral_code="b3", bot_id=None, created_at=NOW - timedelta(hours=20)),
    ]
    db.add_all(buyers)
    await db.flush()

    def deposit(user, kopeks, when, method="platega", type_=TransactionType.DEPOSIT.value):
        return Transaction(
            user_id=user.id,
            type=type_,
            amount_kopeks=kopeks,
            payment_method=method,
            is_completed=True,
            created_at=when,
        )

    db.add_all(
        [
            deposit(buyers[0], 100000, NOW - timedelta(days=10)),  # созрело
            deposit(buyers[1], 50000, NOW - timedelta(hours=20)),  # вчера, не созрело
            deposit(buyers[0], 70000, NOW - timedelta(days=9), method="manual"),  # ручное — не оборот
            deposit(buyers[0], 30000, NOW - timedelta(days=9), method=None),  # реферальное — не оборот
            deposit(buyers[0], 40000, NOW - timedelta(days=9), type_=TransactionType.SUBSCRIPTION_PAYMENT.value),
            deposit(buyers[2], 900000, NOW - timedelta(days=10)),  # чужой пользователь
        ]
    )
    await db.commit()
    return owner_user, owner


@pytest.mark.anyio
@needs_sqlite
async def test_stats_and_earnings(monkeypatch) -> None:
    monkeypatch.setattr(settings, "OWNER_DEFAULT_REVSHARE_PERCENT", 60, raising=False)
    monkeypatch.setattr(settings, "OWNER_PAYOUT_HOLD_DAYS", 7, raising=False)
    db = await _session()
    owner_user, owner = await _owner_world(db)

    stats = await platform.owner_stats(db, owner_user.id, now=NOW)
    assert stats == platform.OwnerStats(active_bots=1, users_total=2, users_yesterday=1)

    earnings = await platform.owner_earnings(db, owner, now=NOW)
    assert earnings.turnover_total == 150000
    assert earnings.turnover_yesterday == 50000
    assert earnings.profit_total == 90000
    assert earnings.profit_yesterday == 30000
    assert earnings.available_kopeks == 60000  # 60% от созревших 1000 ₽

    db.add_all(
        [
            OwnerPayout(owner_id=owner.id, amount_rub_kopeks=20000, amount_usdt=Decimal("2"), fee_usdt=Decimal("3"),
                        cbr_rate=Decimal("100"), rate=Decimal("107"), trc20_address=USDT_CONTRACT, status="paid"),
            OwnerPayout(owner_id=owner.id, amount_rub_kopeks=10000, amount_usdt=Decimal("1"), fee_usdt=Decimal("3"),
                        cbr_rate=Decimal("100"), rate=Decimal("107"), trc20_address=USDT_CONTRACT, status="pending"),
            OwnerPayout(owner_id=owner.id, amount_rub_kopeks=50000, amount_usdt=Decimal("5"), fee_usdt=Decimal("3"),
                        cbr_rate=Decimal("100"), rate=Decimal("107"), trc20_address=USDT_CONTRACT, status="rejected"),
        ]
    )
    owner.revshare_percent = 70
    await db.commit()
    earnings = await platform.owner_earnings(db, owner, now=NOW)
    assert earnings.revshare_percent == 70
    assert earnings.available_kopeks == 70000 - 30000  # отклонённая не вычитается
    await db.close()


@pytest.mark.anyio
@needs_sqlite
async def test_create_payout_checks_available(monkeypatch) -> None:
    monkeypatch.setattr(settings, "OWNER_PAYOUT_FEE_USDT", 3.0, raising=False)
    db = await _session()
    owner_user, owner = await _owner_world(db)
    owner.trc20_address = USDT_CONTRACT
    await db.commit()
    rate = platform.PayoutRate(cbr_rate=Decimal("100"), rate=Decimal("107"))

    # Доступно 600 ₽ = 5.60 USDT.
    assert await platform.create_payout(db, owner.id, Decimal("5.61"), rate) is None
    payout = await platform.create_payout(db, owner.id, Decimal("5.60"), rate)
    assert payout is not None and payout.status == "pending"
    assert payout.amount_rub_kopeks == 59920
    assert payout.trc20_address == USDT_CONTRACT
    # Второй раз то же не вывести.
    assert await platform.create_payout(db, owner.id, Decimal("5.60"), rate) is None

    processed = await platform.process_payout(db, payout.id, paid=False, admin_tg_id=42)
    assert processed.status == "rejected" and processed.processed_by == 42
    assert await platform.process_payout(db, payout.id, paid=True, admin_tg_id=42) is None
    # Отклонённая вернула сумму.
    assert await platform.create_payout(db, owner.id, Decimal("5.60"), rate) is not None
    assert (await platform.payout_recipient(db, payout)).id == owner_user.id
    await db.close()


@pytest.mark.anyio
@needs_sqlite
async def test_bot_occupancy() -> None:
    db = await _session()
    owner_user, _ = await _owner_world(db)
    stranger = User(telegram_id=2, referral_code="stranger")
    db.add(stranger)
    await db.commit()

    assert (await platform.occupancy_by_username(db, "MY_BOT", owner_user.id)).status == "active"
    assert (await platform.occupancy_by_username(db, "gone_bot", owner_user.id)).status == "restore"
    assert (await platform.occupancy_by_username(db, "gone_bot", stranger.id)).status == "foreign"
    assert (await platform.occupancy_by_username(db, "new_bot", owner_user.id)).status == "free"
    assert (await platform.occupancy_by_bot_id(db, OTHER_BOT, owner_user.id)).status == "restore"

    row = (await platform.occupancy_by_bot_id(db, OTHER_BOT, owner_user.id)).row
    await platform.restore_bot(db, row, token="t3", username="gone_bot", title="Gone VPN")
    assert row.enabled and row.token == "t3"
    assert (await platform.occupancy_by_bot_id(db, OTHER_BOT, owner_user.id)).status == "active"

    await platform.disable_bot(db, row)
    assert [r.id for r in await platform.list_owner_bots(db, owner_user.id, enabled_only=True)] == [1]
    await db.close()


@pytest.mark.anyio
@needs_sqlite
async def test_connect_bot_and_mode(monkeypatch) -> None:
    monkeypatch.setattr(settings, "OWNER_BOTS_GROUP", "owners", raising=False)
    db = await _session()
    owner_user, _ = await _owner_world(db)

    row = await platform.connect_bot(
        db,
        owner_user_id=owner_user.id,
        token="t9",
        bot_id=5009,
        username="fresh_bot",
        title="Fresh VPN | быстро",
        logo="data/bot_logos/5009.jpg",
        support="owner_name",
    )
    assert row.is_copycat and row.enabled
    assert row.support == "@owner_name"
    assert row.telegram_bot_id == 5009 and row.title == "Fresh VPN | быстро"
    assert row.group_id is not None

    newcomer = User(telegram_id=3, referral_code="newcomer")
    db.add(newcomer)
    await db.commit()
    # Любой выбор заводит строку: по ней /start понимает, что выбор уже был.
    first = await platform.set_mode(db, newcomer.id, platform.MODE_VPN)
    assert first.mode == "vpn"
    owner = await platform.set_mode(db, newcomer.id, platform.MODE_OWNER)
    assert owner.id == first.id and owner.mode == "owner"
    assert (await platform.set_mode(db, newcomer.id, platform.MODE_VPN)).mode == "vpn"
    await db.close()


@pytest.mark.anyio
@needs_sqlite
async def test_save_owner_prices_creates_own_scheme(monkeypatch) -> None:
    monkeypatch.setattr(settings, "OWNER_PRICE_PERIOD_MULTIPLIERS", "90:2,360:5,720:7", raising=False)
    db = await _session()
    owner_user, _ = await _owner_world(db)
    row = (await platform.list_owner_bots(db, owner_user.id, enabled_only=True))[0]

    schemes.set_snapshot_for_tests([], base={"solo": {30: 32000}, "plus": {30: 49000}, "pro": {30: 69000}})

    async def fake_reload():
        await schemes.load_schemes(db)

    monkeypatch.setattr(schemes, "reload_schemes", fake_reload)
    try:
        await platform.save_owner_prices(db, row, {"solo": 400, "plus": 500, "pro": 700})
        scheme = await schemes.get_scheme_row(db, row.price_scheme_id)
        assert scheme.name == f"owner-{OWNER_BOT}" and scheme.enforce_floor
        tariffs = {t.plan_code: t.period_prices for t in scheme.tariffs}
        assert tariffs["solo"] == {"30": 40000, "90": 80000, "360": 200000, "720": 280000}

        from app.utils import bot_registry

        assert bot_registry.get_price_scheme_id(OWNER_BOT) == scheme.id
        # Второй раз пишет в ту же схему.
        await platform.save_owner_prices(db, row, {"solo": 450, "plus": 500, "pro": 700})
        assert row.price_scheme_id == scheme.id
        await db.refresh(scheme, ["tariffs"])
        assert {t.plan_code: t.period_prices for t in scheme.tariffs}["solo"]["30"] == 45000
    finally:
        from app.utils import bot_registry

        bot_registry.clear()
        schemes.set_snapshot_for_tests([])
        await db.close()
