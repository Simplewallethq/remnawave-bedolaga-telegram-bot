"""Выделенный IP: цены, сквад слота в панели, выдача/продление/удержание/возврат в пул."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import settings  # noqa: E402
from app.database.models import (  # noqa: E402
    Base,
    DedicatedIpSlot,
    Subscription,
    SubscriptionStatus,
    User,
)
from app.services import dedicated_ip_service as dip  # noqa: E402
from app.utils import panel_squads  # noqa: E402

SQUAD_1 = "11111111-1111-1111-1111-111111111111"
SQUAD_2 = "22222222-2222-2222-2222-222222222222"
DEFAULT = "53b1b439-0396-49bc-ba5d-cab4340e6247"


def _aiosqlite_available() -> bool:
    module = sys.modules.get("aiosqlite")
    return module is not None and hasattr(module, "DatabaseError")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(settings, "DEDICATED_IP_ENABLED", True)
    monkeypatch.setattr(settings, "DEDICATED_IP_PRICE_KOPEKS", 29000)
    monkeypatch.setattr(settings, "DEDICATED_IP_HOLD_DAYS", 7)
    monkeypatch.setattr(settings, "DEDICATED_IP_ADMINS_ONLY", False)
    monkeypatch.setattr(settings, "PAID_INTERNAL_SQUAD_UUID", None)
    monkeypatch.setattr(settings, "PRIMARY_BRAND_INTERNAL_SQUAD_UUID", None)
    panel_squads.set_dedicated_ip_pool_squads([])
    yield
    panel_squads.set_dedicated_ip_pool_squads([])


# ---------------------------------------------------------------- цены

def test_price_for_period():
    assert dip.price_for_period_kopeks(30) == 29000
    assert dip.price_for_period_kopeks(90) == 87000
    assert dip.price_for_period_kopeks(360) == 348000
    assert dip.price_for_period_kopeks(0) == 0


def test_price_for_remaining_rounds_up_to_10_rub():
    now = datetime(2026, 11, 23, 12, 0)
    # 80 дней: 290/30×80 = 773,33 → 780 ₽
    assert dip.price_for_remaining_kopeks(now + timedelta(days=80), now) == 78000
    # 30 дней ровно: 290 ₽
    assert dip.price_for_remaining_kopeks(now + timedelta(days=30), now) == 29000
    # часть суток считается днём: 1 день → 9,67 → 10 ₽
    assert dip.price_for_remaining_kopeks(now + timedelta(hours=3), now) == 1000
    assert dip.price_for_remaining_kopeks(now - timedelta(days=1), now) == 0


def test_default_invoice_choice():
    assert dip.default_invoice_choice(None) is False
    assert dip.default_invoice_choice(SimpleNamespace(autorenew=True)) is True
    assert dip.default_invoice_choice(SimpleNamespace(autorenew=False)) is False


# ---------------------------------------------------------------- сквады панели

def test_build_panel_squads_adds_slot_squad_and_strips_pool():
    panel_squads.set_dedicated_ip_pool_squads([SQUAD_1, SQUAD_2])
    user = SimpleNamespace(bot_id=None)
    # Чужой сквад пула в базе (старые данные) вырезается, свой — добавляется в конец.
    sub = SimpleNamespace(is_trial=False, connected_squads=[DEFAULT, SQUAD_2], dedicated_ip_squad_uuid=SQUAD_1)
    assert panel_squads.build_panel_squads(sub, user=user) == [DEFAULT, SQUAD_1]

    sub_without = SimpleNamespace(is_trial=False, connected_squads=[DEFAULT, SQUAD_1], dedicated_ip_squad_uuid=None)
    assert panel_squads.build_panel_squads(sub_without, user=user) == [DEFAULT]


def test_strip_removes_pool_squads_from_panel_import():
    panel_squads.set_dedicated_ip_pool_squads([SQUAD_1])
    assert panel_squads.strip_bot_managed_squads([DEFAULT, SQUAD_1]) == [DEFAULT]


def test_feature_disabled_for_copycat(monkeypatch):
    from app.utils import bot_registry

    monkeypatch.setattr(bot_registry, "is_copycat_user", lambda user: True)
    assert dip.is_feature_enabled_for(SimpleNamespace(telegram_id=1, bot_id=1)) is False


def test_subscription_eligibility():
    future = datetime.utcnow() + timedelta(days=10)
    base = dict(plan_id=1, is_trial=False, is_paid_trial=False, is_partner=False,
                status=SubscriptionStatus.ACTIVE.value, end_date=future)
    assert dip.subscription_is_eligible(SimpleNamespace(**base)) is True
    assert dip.subscription_is_eligible(SimpleNamespace(**{**base, "is_trial": True})) is False
    assert dip.subscription_is_eligible(SimpleNamespace(**{**base, "plan_id": None})) is False
    assert dip.subscription_is_eligible(SimpleNamespace(**{**base, "is_paid_trial": True})) is False
    assert dip.subscription_is_eligible(
        SimpleNamespace(**{**base, "end_date": datetime.utcnow() - timedelta(days=1)})
    ) is False
    assert dip.subscription_is_eligible(None) is False


# ---------------------------------------------------------------- база

pytestmark_db = pytest.mark.skipif(not _aiosqlite_available(), reason="настоящий aiosqlite недоступен")


async def _session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)()


async def _user_with_subscription(db: AsyncSession, *, telegram_id: int, balance: int, days: int = 30):
    user = User(telegram_id=telegram_id, balance_kopeks=balance, language="ru")
    db.add(user)
    await db.flush()
    sub = Subscription(
        user_id=user.id,
        status=SubscriptionStatus.ACTIVE.value,
        is_trial=False,
        end_date=datetime.utcnow() + timedelta(days=days),
        connected_squads=[DEFAULT],
    )
    db.add(sub)
    await db.commit()
    return user, sub


@pytest.fixture
def no_side_effects(monkeypatch):
    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(dip, "_sync_panel", _noop)
    monkeypatch.setattr(dip, "clear_invoice_choice", _noop)
    monkeypatch.setattr(dip, "_notify_user", _noop)
    monkeypatch.setattr(dip, "notify_low_stock_if_needed", _noop)

    async def _no_source(user_id, default):
        return default

    monkeypatch.setattr(dip, "_pop_source", _no_source)


@pytestmark_db
@pytest.mark.anyio
async def test_charge_assign_hold_return_and_release(no_side_effects):
    db = await _session()
    try:
        await dip.seed_slots(db, [
            {"slot_no": 1, "ip": "45.136.136.230", "squad_uuid": SQUAD_1},
            {"slot_no": 2, "ip": "45.136.136.231", "squad_uuid": SQUAD_2},
        ])
        assert panel_squads.get_dedicated_ip_pool_squads() == {SQUAD_1, SQUAD_2}
        user, sub = await _user_with_subscription(db, telegram_id=100, balance=100000)

        slot = await dip.charge_and_apply(db, user, sub, price_kopeks=29000)
        assert slot.status == DedicatedIpSlot.STATUS_ACTIVE
        assert slot.user_id == user.id
        assert slot.paid_until == sub.end_date
        assert sub.dedicated_ip_squad_uuid == slot.squad_uuid
        await db.refresh(user)
        assert user.balance_kopeks == 71000
        assert await dip.count_free_slots(db) == 1

        # Оплата IP кончилась при включённом продлении → удержание, сквад снят.
        slot.paid_until = datetime.utcnow() - timedelta(minutes=1)
        await db.commit()
        counters = await dip.process_expirations(db)
        assert counters == {"hold": 1, "released": 0}
        await db.refresh(slot)
        await db.refresh(sub)
        assert slot.status == DedicatedIpSlot.STATUS_HOLD
        assert slot.user_id == user.id
        assert sub.dedicated_ip_squad_uuid is None

        # Вернул IP в удержании — тот же адрес.
        again = await dip.charge_and_apply(db, user, sub, price_kopeks=1000)
        assert again.id == slot.id
        assert again.status == DedicatedIpSlot.STATUS_ACTIVE
        await db.refresh(sub)
        assert sub.dedicated_ip_squad_uuid == slot.squad_uuid

        # Выключил продление — по окончании оплаты слот сразу в пул.
        await dip.set_autorenew(db, again, False)
        again.paid_until = datetime.utcnow() - timedelta(minutes=1)
        await db.commit()
        counters = await dip.process_expirations(db)
        assert counters == {"hold": 0, "released": 1}
        await db.refresh(again)
        assert again.status == DedicatedIpSlot.STATUS_FREE
        assert again.user_id is None
        assert again.released_at is not None
        assert await dip.get_user_slot(db, user.id) is None
    finally:
        await db.close()


@pytestmark_db
@pytest.mark.anyio
async def test_hold_expires_back_to_pool(no_side_effects):
    db = await _session()
    try:
        await dip.seed_slots(db, [{"slot_no": 1, "ip": "45.136.136.230", "squad_uuid": SQUAD_1}])
        user, sub = await _user_with_subscription(db, telegram_id=101, balance=29000)
        slot = await dip.charge_and_apply(db, user, sub, price_kopeks=29000)
        await dip.put_on_hold(db, slot)
        slot.hold_until = datetime.utcnow() - timedelta(minutes=1)
        await db.commit()
        counters = await dip.process_expirations(db)
        assert counters["released"] == 1
        await db.refresh(slot)
        assert slot.status == DedicatedIpSlot.STATUS_FREE
    finally:
        await db.close()


@pytestmark_db
@pytest.mark.anyio
async def test_sold_out_and_insufficient_balance_charge_nothing(no_side_effects):
    db = await _session()
    try:
        await dip.seed_slots(db, [{"slot_no": 1, "ip": "45.136.136.230", "squad_uuid": SQUAD_1}])
        first, first_sub = await _user_with_subscription(db, telegram_id=200, balance=29000)
        second, second_sub = await _user_with_subscription(db, telegram_id=201, balance=29000)
        poor, poor_sub = await _user_with_subscription(db, telegram_id=202, balance=100)

        with pytest.raises(dip.InsufficientBalanceError):
            await dip.charge_and_apply(db, poor, poor_sub, price_kopeks=29000)
        await db.refresh(poor)
        assert poor.balance_kopeks == 100

        await dip.charge_and_apply(db, first, first_sub, price_kopeks=29000)
        assert await dip.count_free_slots(db) == 0
        assert await dip.is_on_sale(db, second) is False
        assert await dip.is_on_sale(db, first) is True  # свой IP — экран «Мой IP» остаётся

        with pytest.raises(dip.NoFreeSlotError):
            await dip.charge_and_apply(db, second, second_sub, price_kopeks=29000)
        await db.refresh(second)
        assert second.balance_kopeks == 29000

        addon = await dip.invoice_addon(db, second, 30)
        assert addon["available"] is False
    finally:
        await db.close()


@pytestmark_db
@pytest.mark.anyio
async def test_renewal_addon_only_when_autorenew(no_side_effects):
    db = await _session()
    try:
        await dip.seed_slots(db, [{"slot_no": 1, "ip": "45.136.136.230", "squad_uuid": SQUAD_1}])
        user, sub = await _user_with_subscription(db, telegram_id=300, balance=29000)
        assert await dip.renewal_addon_kopeks(db, sub, 90) == 0
        slot = await dip.charge_and_apply(db, user, sub, price_kopeks=29000)
        assert await dip.renewal_addon_kopeks(db, sub, 90) == 87000
        await dip.set_autorenew(db, slot, False)
        assert await dip.renewal_addon_kopeks(db, sub, 90) == 0
    finally:
        await db.close()


@pytestmark_db
@pytest.mark.anyio
async def test_seed_is_additive_and_rejects_mismatch(no_side_effects):
    db = await _session()
    try:
        result = await dip.seed_slots(db, [{"slot_no": 1, "ip": "45.136.136.230", "squad_uuid": SQUAD_1}])
        assert result == {"created": 1, "updated": 0}
        result = await dip.seed_slots(db, [
            {"slot_no": 1, "ip": "45.136.136.230", "squad_uuid": SQUAD_1, "host_uuid": "h1"},
            {"slot_no": 2, "ip": "45.136.136.231", "squad_uuid": SQUAD_2},
        ])
        assert result == {"created": 1, "updated": 1}
        with pytest.raises(dip.DedicatedIpError):
            await dip.seed_slots(db, [{"slot_no": 1, "ip": "45.136.136.240", "squad_uuid": SQUAD_1}])
    finally:
        await db.close()


# ---------------------------------------------------------------- состав счёта

@pytest.mark.anyio
async def test_invoice_snapshot_fixes_dedicated_ip_composition(monkeypatch):
    from app.services import user_cart_service as cart_module
    from app.services.tariff_partial_payment_service import (
        COMPOSITION_KIND,
        SNAPSHOT_METADATA_KEY,
        build_invoice_checkout_snapshot,
        extract_checkout_snapshot,
    )

    cart = {"cart_mode": "tariff", "tariff_op": "renew", "intent": True, "plan_id": 3,
            "period_days": 90, "total_price": 99000, "dedicated_ip": True, "dedicated_ip_kopeks": 87000}

    async def fake_get_user_cart(user_id):
        return cart

    monkeypatch.setattr(cart_module.user_cart_service, "get_user_cart", fake_get_user_cart)

    snapshot = await build_invoice_checkout_snapshot(1, 186000)
    assert snapshot["kind"] == COMPOSITION_KIND
    assert snapshot["dedicated_ip"] is True
    assert snapshot["dedicated_ip_kopeks"] == 87000
    payment = SimpleNamespace(metadata_json={SNAPSHOT_METADATA_KEY: snapshot})
    assert extract_checkout_snapshot(payment) == snapshot

    # Отдельная покупка IP: сумма IP — вся корзина.
    cart.clear()
    cart.update({"cart_mode": "dedicated_ip", "intent": True, "total_price": 78000})
    snapshot = await build_invoice_checkout_snapshot(1, 78000)
    assert snapshot["dedicated_ip"] is True and snapshot["dedicated_ip_kopeks"] == 78000

    # IP не предлагался — снимка нет (как раньше).
    cart.clear()
    cart.update({"cart_mode": "tariff", "tariff_op": "renew", "intent": True, "plan_id": 3, "total_price": 99000})
    assert await build_invoice_checkout_snapshot(1, 99000) is None


@pytest.mark.anyio
async def test_paid_invoice_without_ip_does_not_grant_ip(monkeypatch):
    """Человек включил IP после выставления счёта, а оплатил старый счёт без IP."""
    from app.services import subscription_auto_purchase_service as auto

    cart = {"cart_mode": "tariff", "tariff_op": "renew", "intent": True, "plan_id": 3,
            "period_days": 30, "dedicated_ip": True, "dedicated_ip_kopeks": 29000}

    async def fake_get_user_cart(user_id):
        return dict(cart)

    seen = {}

    async def fake_tariff(db, user, cart_data, *, bot=None):
        seen.update(cart_data)
        return True

    monkeypatch.setattr(auto.user_cart_service, "get_user_cart", fake_get_user_cart)
    monkeypatch.setattr(auto, "_auto_tariff_purchase", fake_tariff)
    user = SimpleNamespace(id=1, telegram_id=1, bot_id=None)
    snapshot = {"kind": "cart_composition", "dedicated_ip": False, "dedicated_ip_kopeks": 0}

    assert await auto.auto_purchase_saved_cart_after_topup(None, user, checkout_snapshot=snapshot) is True
    assert "dedicated_ip" not in seen

    seen.clear()
    cart["dedicated_ip"] = False
    snapshot = {"kind": "cart_composition", "dedicated_ip": True, "dedicated_ip_kopeks": 29000}
    assert await auto.auto_purchase_saved_cart_after_topup(None, user, checkout_snapshot=snapshot) is True
    assert seen["dedicated_ip"] is True and seen["dedicated_ip_kopeks"] == 29000
