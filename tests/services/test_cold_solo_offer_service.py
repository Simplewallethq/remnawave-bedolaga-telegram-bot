from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.database.models import DiscountOffer
from app.services.cold_solo_offer_service import cold_solo_offer_service


def test_offer_expires_at_is_2200_msk() -> None:
    now = datetime(2026, 7, 6, 8, 0, tzinfo=timezone.utc)  # 11:00 MSK

    expires_at = cold_solo_offer_service.offer_expires_at(now)

    assert expires_at == datetime(2026, 7, 6, 19, 0)


def test_validate_offer_for_solo_year() -> None:
    offer = DiscountOffer(
        notification_type=cold_solo_offer_service.NOTIFICATION_TYPE,
        effect_type=cold_solo_offer_service.EFFECT_TYPE,
        is_active=True,
        expires_at=datetime(2099, 1, 1),
        extra_data=cold_solo_offer_service.build_extra_data(),
    )

    assert cold_solo_offer_service.validate_offer_for_plan(
        offer,
        plan_code="solo",
        period_days=360,
    )

    assert not cold_solo_offer_service.validate_offer_for_plan(
        offer,
        plan_code="plus",
        period_days=360,
    )
    assert not cold_solo_offer_service.validate_offer_for_plan(
        offer,
        plan_code="solo",
        period_days=30,
    )


def test_offer_extra_contains_fixed_price_payload() -> None:
    extra = cold_solo_offer_service.build_extra_data()

    assert extra["plan_code"] == "solo"
    assert extra["period_days"] == 360
    assert extra["price_kopeks"] == 99_000
    assert extra["original_price_kopeks"] == 156_000


# ------------------------------------------- когорта «за 1 ₽» получает те же офферы


class _CapturingDb:
    def __init__(self, count: int = 0) -> None:
        self.count = count
        self.sql: list = []

    async def execute(self, statement):
        self.sql.append(str(statement.compile(compile_kwargs={"literal_binds": True})))
        return SimpleNamespace(scalar=lambda: self.count)


@pytest.mark.anyio
async def test_has_paid_ignores_symbolic_one_ruble_payment(monkeypatch):
    from app.config import settings
    from app.services.cold_solo_offer_service import ColdSoloOfferService

    monkeypatch.setattr(settings, "TRIAL_PAID_OFFER_PRICE_KOPEKS", 100, raising=False)
    db = _CapturingDb(count=0)
    user = SimpleNamespace(id=7, has_had_paid_subscription=False, has_made_first_topup=False)

    assert await ColdSoloOfferService().has_paid(db, user) is False
    assert "transactions.amount_kopeks > 100" in db.sql[0]

    assert await ColdSoloOfferService().has_successful_subscription_purchase(db, user) is False
    assert "transactions.amount_kopeks > 100" in db.sql[1]


@pytest.mark.anyio
async def test_paid_trial_subscription_is_initially_eligible(monkeypatch):
    from datetime import datetime, timedelta

    from app.services.cold_solo_offer_service import ColdSoloOfferService

    service = ColdSoloOfferService()
    monkeypatch.setattr(service, "is_allowed_user", lambda user_id: True)

    async def no(*_args, **_kwargs):
        return False

    async def none(*_args, **_kwargs):
        return None

    monkeypatch.setattr(service, "has_paid", no)
    monkeypatch.setattr(service, "has_any_invoice", no)
    monkeypatch.setattr(service, "get_any_offer", none)

    ended = datetime.utcnow() - timedelta(hours=11)
    paid_trial = SimpleNamespace(is_trial=False, is_paid_trial=True, end_date=ended)
    user = SimpleNamespace(id=7, telegram_id=1, status="active", subscription=paid_trial)
    assert await service.is_initially_eligible(object(), user) is True

    regular_paid = SimpleNamespace(is_trial=False, is_paid_trial=False, end_date=ended)
    user.subscription = regular_paid
    assert await service.is_initially_eligible(object(), user) is False
