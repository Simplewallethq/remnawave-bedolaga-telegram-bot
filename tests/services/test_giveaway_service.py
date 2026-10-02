"""Розыгрыш: подсчёт билетов, выборки друзей/оплат/тарифа и снимок участника."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.enums import ChatMemberStatus
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import settings  # noqa: E402
from app.database.models import (  # noqa: E402
    Base,
    GiveawayEntry,
    PaymentMethod,
    Subscription,
    SubscriptionPlan,
    SubscriptionStatus,
    Transaction,
    TransactionType,
    User,
    UserStatus,
)
from app.services.giveaway_service import (  # noqa: E402
    GiveawayProgress,
    _parse_moment,
    giveaway_service,
)

# Окно розыгрыша в UTC: 2026-10-02 21:00 … 2026-10-20 20:59:59.
START = "2026-10-03T00:00:00+03:00"
END = "2026-10-20T23:59:59+03:00"
INSIDE = datetime(2026, 10, 10, 12, 0)
BEFORE = datetime(2026, 10, 2, 20, 0)
AFTER = datetime(2026, 10, 21, 0, 0)


def _aiosqlite_available() -> bool:
    module = sys.modules.get("aiosqlite")
    if module is None:
        try:
            import aiosqlite  # noqa: F401
        except ImportError:
            return False
        module = sys.modules.get("aiosqlite")
    return module is not None and hasattr(module, "DatabaseError")


needs_db = pytest.mark.skipif(not _aiosqlite_available(), reason="настоящий aiosqlite недоступен")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _giveaway_window(monkeypatch):
    monkeypatch.setattr(settings, "GIVEAWAY_ENABLED", True)
    monkeypatch.setattr(settings, "GIVEAWAY_CODE", "test_giveaway")
    monkeypatch.setattr(settings, "GIVEAWAY_START_AT", START)
    monkeypatch.setattr(settings, "GIVEAWAY_END_AT", END)
    monkeypatch.setattr(settings, "GIVEAWAY_MIN_FRIEND_PAYMENT_KOPEKS", 10000)


async def _make_session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)()


_next_tg_id = iter(range(1000, 100000))


async def _user(db: AsyncSession, *, referrer: User | None = None, created_at=INSIDE, **extra) -> User:
    user = User(
        telegram_id=next(_next_tg_id),
        referred_by_id=referrer.id if referrer else None,
        created_at=created_at,
        **extra,
    )
    db.add(user)
    await db.flush()
    return user


def _payment(user: User, amount: int, *, at=INSIDE, completed=True, method=None) -> Transaction:
    return Transaction(
        user_id=user.id,
        type=TransactionType.SUBSCRIPTION_PAYMENT.value,
        amount_kopeks=amount,
        is_completed=completed,
        payment_method=method,
        created_at=at,
    )


# ---------------------------------------------------------------- билеты


def test_tickets_sum_every_completed_row() -> None:
    progress = GiveawayProgress(
        channel_subscribed=True, invited_count=9, invited_paid_count=2, plan_code="plus"
    )
    # канал 1 + друзья 2+4+6 + Plus 3 + 2 оплаты × 4
    assert progress.total_tickets == 1 + 12 + 3 + 8


def test_invite_tiers_between_thresholds() -> None:
    assert GiveawayProgress(None, 2, 0, None).invite_tickets == 0
    assert GiveawayProgress(None, 5, 0, None).invite_tickets == 2
    assert GiveawayProgress(None, 8, 0, None).invite_tickets == 6


def test_unchecked_channel_gives_no_ticket() -> None:
    assert GiveawayProgress(None, 0, 0, None).total_tickets == 0


def test_window_is_parsed_to_naive_utc() -> None:
    assert _parse_moment(START) == datetime(2026, 10, 2, 21, 0)
    assert giveaway_service.ends_at == datetime(2026, 10, 20, 20, 59, 59)


def test_is_running_only_inside_window(monkeypatch) -> None:
    assert giveaway_service.is_running(INSIDE)
    assert not giveaway_service.is_running(BEFORE)
    assert not giveaway_service.is_running(AFTER)
    monkeypatch.setattr(settings, "GIVEAWAY_ENABLED", False)
    assert not giveaway_service.is_running(INSIDE)


# ---------------------------------------------------------------- друзья


@pytest.mark.anyio
@needs_db
async def test_only_new_friends_inside_window_count() -> None:
    db = await _make_session()
    me = await _user(db, created_at=BEFORE)
    other = await _user(db, created_at=BEFORE)

    await _user(db, referrer=me)
    await _user(db, referrer=me)
    await _user(db, referrer=me, created_at=BEFORE)  # пришёл до розыгрыша
    await _user(db, referrer=me, created_at=AFTER)  # пришёл после
    await _user(db, referrer=me, status=UserStatus.DELETED.value)
    await _user(db, referrer=other)
    await db.commit()

    assert await giveaway_service.count_invited(db, me.id) == 2
    await db.close()


@pytest.mark.anyio
@needs_db
async def test_paid_friends_need_real_payment_inside_window() -> None:
    db = await _make_session()
    me = await _user(db, created_at=BEFORE)

    paid = await _user(db, referrer=me)
    paid_twice = await _user(db, referrer=me)
    apple = await _user(db, referrer=me)
    one_rouble = await _user(db, referrer=me)
    pending = await _user(db, referrer=me)
    paid_too_early = await _user(db, referrer=me)
    old_friend = await _user(db, referrer=me, created_at=BEFORE)

    db.add_all([
        _payment(paid, 32000),
        _payment(paid_twice, 32000),
        _payment(paid_twice, 49000),
        _payment(apple, 0, method=PaymentMethod.APPLE_IAP.value),
        _payment(one_rouble, 100),
        _payment(pending, 32000, completed=False),
        _payment(paid_too_early, 32000, at=BEFORE),
        _payment(old_friend, 32000),
    ])
    await db.commit()

    assert await giveaway_service.count_invited_paid(db, me.id) == 3
    await db.close()


# ---------------------------------------------------------------- тариф


async def _with_subscription(db: AsyncSession, *, plan_code: str | None, **fields) -> User:
    user = await _user(db)
    plan_id = None
    if plan_code:
        plan = SubscriptionPlan(code=plan_code, display_name=plan_code.title())
        db.add(plan)
        await db.flush()
        plan_id = plan.id
    values = dict(
        status=SubscriptionStatus.ACTIVE.value,
        is_trial=False,
        end_date=datetime.utcnow() + timedelta(days=30),
    )
    values.update(fields)
    db.add(Subscription(user_id=user.id, plan_id=plan_id, **values))
    await db.commit()
    return user


@pytest.mark.anyio
@needs_db
@pytest.mark.parametrize(
    ("plan_code", "fields", "expected"),
    [
        ("pro", {}, "pro"),
        (None, {}, "solo"),  # подписка до перехода на тарифы
        ("plus", {"is_trial": True}, None),
        ("solo", {"is_paid_trial": True}, None),
        ("plus", {"end_date": datetime(2020, 1, 1)}, None),
        ("plus", {"status": SubscriptionStatus.EXPIRED.value}, None),
        ("family", {}, None),  # тариф вне розыгрыша
    ],
)
async def test_active_plan_code(plan_code, fields, expected) -> None:
    db = await _make_session()
    user = await _with_subscription(db, plan_code=plan_code, **fields)

    assert await giveaway_service.active_plan_code(db, user.id) == expected
    await db.close()


@pytest.mark.anyio
@needs_db
async def test_no_subscription_means_no_plan() -> None:
    db = await _make_session()
    user = await _user(db)
    await db.commit()

    assert await giveaway_service.active_plan_code(db, user.id) is None
    await db.close()


# ---------------------------------------------------------------- канал


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("member", "expected"),
    [
        (SimpleNamespace(status=ChatMemberStatus.MEMBER), True),
        (SimpleNamespace(status=ChatMemberStatus.CREATOR), True),
        (SimpleNamespace(status=ChatMemberStatus.LEFT), False),
        (SimpleNamespace(status=ChatMemberStatus.KICKED), False),
        (SimpleNamespace(status=ChatMemberStatus.RESTRICTED, is_member=True), True),
        (SimpleNamespace(status=ChatMemberStatus.RESTRICTED, is_member=False), False),
    ],
)
async def test_check_channel_statuses(member, expected) -> None:
    bot = SimpleNamespace(get_chat_member=AsyncMock(return_value=member))

    assert await giveaway_service.check_channel(bot, 42) is expected


@pytest.mark.anyio
async def test_check_channel_api_error_is_unknown() -> None:
    bot = SimpleNamespace(get_chat_member=AsyncMock(side_effect=RuntimeError("not admin")))

    assert await giveaway_service.check_channel(bot, 42) is None


# ---------------------------------------------------------------- снимок


@pytest.mark.anyio
@needs_db
async def test_save_entry_keeps_one_row_per_user() -> None:
    db = await _make_session()
    user = await _user(db)
    await db.commit()

    await giveaway_service.save_entry(db, user.id, GiveawayProgress(True, 0, 0, None))
    await giveaway_service.save_entry(db, user.id, GiveawayProgress(True, 3, 1, "pro"))

    rows = (await db.execute(select(func.count(GiveawayEntry.id)))).scalar()
    entry = await giveaway_service.get_entry(db, user.id)
    assert rows == 1
    assert (entry.invited_count, entry.invited_paid_count, entry.plan_code) == (3, 1, "pro")
    assert entry.tickets == 1 + 2 + 4 + 4
    await db.close()


# ---------------------------------------------------------------- админка


@pytest.mark.anyio
@needs_db
async def test_summary_and_ranking_for_admin() -> None:
    db = await _make_session()
    first = await _user(db, username="first")
    second = await _user(db, username="second")
    third = await _user(db, username="third")
    await db.commit()

    await giveaway_service.save_entry(db, first.id, GiveawayProgress(True, 3, 1, "pro"))
    await giveaway_service.save_entry(db, second.id, GiveawayProgress(False, 0, 0, None))
    await giveaway_service.save_entry(db, third.id, GiveawayProgress(True, 0, 0, "plus"))
    # Запись другого розыгрыша в сводку не попадает.
    db.add(GiveawayEntry(giveaway_code="other", user_id=second.id, tickets=100))
    await db.commit()

    summary = await giveaway_service.summary(db)
    ranked = await giveaway_service.ranked_entries(db)

    assert summary.participants == 3
    assert summary.tickets == (1 + 2 + 4 + 4) + 0 + (1 + 3)
    assert summary.channel_subscribed == 2
    assert (summary.invited, summary.invited_paid) == (3, 1)
    assert summary.plans == {"solo": 0, "plus": 1, "pro": 1}
    assert [user.username for _, user in ranked] == ["first", "third", "second"]
    assert len(await giveaway_service.ranked_entries(db, limit=1)) == 1
    await db.close()
