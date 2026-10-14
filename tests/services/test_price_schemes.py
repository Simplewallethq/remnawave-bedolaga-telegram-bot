"""Ценовые схемы витрин: резолв по боту, фоллбек на цены основного, пол цен."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.branding import brand_scope
from app.config import settings
from app.services import price_scheme_service as schemes
from app.services.plan_pricing_service import (
    get_lowest_monthly_price,
    plan_device_limit,
    resolve_pricing_cohort,
    select_prices_for_cohort,
)
from app.utils import bot_registry

BOT_WITH_SCHEME = 3001
BOT_WITHOUT_SCHEME = 3002

BASE = {
    "solo": {30: 32000, 90: 57000, 180: 89000, 360: 156000, 720: 216000},
    "plus": {30: 49000, 90: 78000, 360: 192000, 720: 264000},
}


def _plan(code: str, plan_id: int, device_limit: int):
    prices = [
        SimpleNamespace(period_days=days, price_kopeks=price, audience="new")
        for days, price in BASE.get(code, {}).items()
    ]
    return SimpleNamespace(id=plan_id, code=code, device_limit=device_limit, prices=prices)


@pytest.fixture
def snapshot(monkeypatch):
    monkeypatch.setattr(settings, "OWNER_PRICE_FLOOR_SCHEME", "owner-floor", raising=False)
    owner = schemes.SchemeSnapshot(
        id=1,
        name="owner-3001",
        trial_days=5,
        enforce_floor=True,
        tariffs={
            # 90 дней ниже пола — должен подняться до 57000.
            "solo": schemes.SchemeTariff(device_limit=2, prices={30: 50000, 90: 1000}),
        },
    )
    schemes.set_snapshot_for_tests([owner], plan_codes={11: "solo", 12: "plus"}, base=BASE)
    bot_registry.clear()
    bot_registry.set_price_scheme(BOT_WITH_SCHEME, 1)
    yield
    bot_registry.clear()
    schemes.set_snapshot_for_tests([])


def test_cohort_follows_bot_of_the_update(snapshot):
    user = SimpleNamespace(bot_id=None, created_at=None, tariff_pricing_cohort_override=None)
    with brand_scope(BOT_WITH_SCHEME):
        assert resolve_pricing_cohort(user) == "scheme:1"
    with brand_scope(BOT_WITHOUT_SCHEME):
        assert resolve_pricing_cohort(user) == "legacy"
    # Вне апдейта — бот регистрации.
    assert resolve_pricing_cohort(SimpleNamespace(bot_id=BOT_WITH_SCHEME)) == "scheme:1"


def test_scheme_prices_fall_back_to_new_and_respect_floor(snapshot):
    solo = _plan("solo", 11, 1)
    prices = select_prices_for_cohort(solo, "scheme:1")
    # Своя месячная, 90 — подтянута до пола, 360/720 — цены основного, 180 не продаётся.
    assert prices == {30: 50000, 90: 57000, 360: 156000, 720: 216000}
    assert get_lowest_monthly_price(solo, "scheme:1") == 50000

    # Тарифа нет в схеме — цены основного для новых.
    plus = _plan("plus", 12, 3)
    assert select_prices_for_cohort(plus, "scheme:1") == BASE["plus"]

    # Схема пропала из снимка — тоже цены основного.
    assert select_prices_for_cohort(solo, "scheme:99")[30] == 32000


def test_scheme_device_limit_and_trial(snapshot):
    solo = _plan("solo", 11, 1)
    assert plan_device_limit(solo, "scheme:1") == 2
    assert plan_device_limit(solo, "new") == 1
    assert plan_device_limit(_plan("plus", 12, 3), "scheme:1") == 3

    assert schemes.trial_days_for_user(SimpleNamespace(bot_id=BOT_WITH_SCHEME)) == 5
    assert schemes.trial_days_for_user(SimpleNamespace(bot_id=BOT_WITHOUT_SCHEME)) == settings.TRIAL_DURATION_DAYS


def test_floor_scheme_overrides_base_prices(snapshot):
    floor = schemes.SchemeSnapshot(
        id=2,
        name="owner-floor",
        trial_days=None,
        enforce_floor=False,
        tariffs={"solo": schemes.SchemeTariff(device_limit=None, prices={30: 20000, 90: 40000})},
    )
    owner = schemes.get_scheme(1)
    schemes.set_snapshot_for_tests([owner, floor], plan_codes={11: "solo"}, base=BASE)
    assert schemes.floor_prices("solo") == {30: 20000, 90: 40000}
    # Пол из схемы ниже цен основного: 90 дней схемы (1000) поднимается до 40000.
    assert select_prices_for_cohort(_plan("solo", 11, 1), "scheme:1")[90] == 40000


def test_derived_prices_use_multipliers_and_floor(snapshot, monkeypatch):
    monkeypatch.setattr(settings, "OWNER_PRICE_PERIOD_MULTIPLIERS", "90:1.78,360:4.875,720:6.75", raising=False)
    # 400 ₽: 712, 1950, 2700 — выше пола основного бота.
    assert schemes.derive_period_prices(400, "solo") == {30: 40000, 90: 71200, 360: 195000, 720: 270000}
    # 321 ₽: 571.38 → 572 вверх; 1564.875 → 1565; 2166.75 → 2167.
    assert schemes.derive_period_prices(321, "solo") == {30: 32100, 90: 57200, 360: 156500, 720: 216700}
    # Ниже пола производные подтягиваются к нему.
    assert schemes.derive_period_prices(100, "solo") == {30: 32000, 90: 57000, 360: 156000, 720: 216000}


def test_clone_scheme_copies_effective_prices(snapshot):
    clone = schemes.clone_scheme(schemes.get_scheme(1), "owner-new", enforce_floor=True)
    tariffs = {tariff.plan_code: tariff for tariff in clone.tariffs}
    assert tariffs["solo"].period_prices == {"30": 50000, "90": 57000, "360": 156000, "720": 216000}
    assert tariffs["solo"].device_limit == 2
    assert tariffs["plus"].period_prices == {"30": 49000, "90": 78000, "360": 192000, "720": 264000}
    assert clone.enforce_floor is True and clone.trial_days == 5

    from_base = schemes.clone_scheme(None, "fresh", enforce_floor=False)
    assert {t.plan_code for t in from_base.tariffs} == {"solo", "plus"}
