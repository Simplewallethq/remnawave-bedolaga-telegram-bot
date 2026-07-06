from datetime import datetime, timezone

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
