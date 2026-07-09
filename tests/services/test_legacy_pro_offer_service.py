from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.database.models import DiscountOffer
from app.services.interactive_notification_service import interactive_notification_service
import app.services.legacy_pro_offer_service as legacy_pro_offer_module
from app.services.legacy_pro_offer_service import legacy_pro_offer_service
from app.services.plan_pricing_service import resolve_pricing_cohort


def test_csv_members_include_ac_and_b_offers() -> None:
    ac_members = legacy_pro_offer_service.list_members(legacy_pro_offer_service.AC_GROUPS)
    b_members = legacy_pro_offer_service.list_members(legacy_pro_offer_service.B_GROUPS)

    assert len(ac_members) == 819
    assert len(b_members) == 118
    assert {member.price_kopeks for member in ac_members} == {129_000}
    assert {member.price_kopeks for member in b_members} == {99_000}


def test_build_extra_data_contains_fixed_pro_payload() -> None:
    member = legacy_pro_offer_service.list_members(legacy_pro_offer_service.AC_GROUPS)[0]

    extra = legacy_pro_offer_service.build_extra_data(member)

    assert extra["plan_code"] == "pro"
    assert extra["period_days"] == 360
    assert extra["price_kopeks"] == 129_000
    assert extra["original_price_kopeks"] == 228_000
    assert extra["offer_type"] == legacy_pro_offer_service.NOTIFICATION_TYPE_1290


def test_b_group_uses_990_notification_type() -> None:
    member = legacy_pro_offer_service.list_members(legacy_pro_offer_service.B_GROUPS)[0]

    assert member.price_kopeks == 99_000
    assert legacy_pro_offer_service.notification_type_for_member(member) == (
        legacy_pro_offer_service.NOTIFICATION_TYPE_990
    )


def test_validate_offer_for_pro_year() -> None:
    member = legacy_pro_offer_service.list_members(legacy_pro_offer_service.AC_GROUPS)[0]
    offer = DiscountOffer(
        notification_type=legacy_pro_offer_service.NOTIFICATION_TYPE_1290,
        effect_type=legacy_pro_offer_service.EFFECT_TYPE,
        is_active=True,
        expires_at=datetime(2099, 1, 1),
        extra_data=legacy_pro_offer_service.build_extra_data(member),
    )

    assert legacy_pro_offer_service.validate_offer_for_plan(
        offer,
        plan_code="pro",
        period_days=360,
    )
    assert not legacy_pro_offer_service.validate_offer_for_plan(
        offer,
        plan_code="solo",
        period_days=360,
    )
    assert not legacy_pro_offer_service.validate_offer_for_plan(
        offer,
        plan_code="pro",
        period_days=30,
    )


def test_pricing_cohort_override_wins_over_registration_date() -> None:
    old_user = SimpleNamespace(
        created_at=datetime(2026, 5, 1),
        tariff_pricing_cohort_override="new",
    )
    new_user = SimpleNamespace(
        created_at=datetime(2026, 7, 1),
        tariff_pricing_cohort_override="legacy",
    )

    assert resolve_pricing_cohort(old_user) == "new"
    assert resolve_pricing_cohort(new_user) == "legacy"


def test_invalid_pricing_cohort_override_falls_back_to_cutoff() -> None:
    old_user = SimpleNamespace(
        created_at=datetime(2026, 5, 1),
        tariff_pricing_cohort_override="unexpected",
    )

    assert resolve_pricing_cohort(old_user) == "legacy"


async def test_mark_claimed_sets_new_pricing_cohort_override(monkeypatch) -> None:
    db = AsyncMock()
    mark_offer_claimed = AsyncMock()
    monkeypatch.setattr(legacy_pro_offer_module, "mark_offer_claimed", mark_offer_claimed)
    offer = DiscountOffer(
        id=55,
        user_id=123,
        notification_type=legacy_pro_offer_service.NOTIFICATION_TYPE_1290,
        effect_type=legacy_pro_offer_service.EFFECT_TYPE,
        is_active=True,
        expires_at=datetime(2099, 1, 1),
        extra_data={"price_kopeks": 129_000, "csv_group": legacy_pro_offer_service.GROUP_A},
    )

    await legacy_pro_offer_service.mark_claimed_after_purchase(db, offer)

    db.execute.assert_awaited_once()
    statement = db.execute.await_args.args[0]
    assert "tariff_pricing_cohort_override" in str(statement)
    mark_offer_claimed.assert_awaited_once()


def test_interactive_service_keeps_recurring_and_adds_dated_slots() -> None:
    slot_by_key = {slot.key: slot for slot in interactive_notification_service.SLOTS}

    assert slot_by_key[legacy_pro_offer_service.AC_SLOT_1].is_one_off
    assert slot_by_key[legacy_pro_offer_service.B_SLOT_1].is_one_off
    assert slot_by_key[legacy_pro_offer_service.AC_SLOT_1].run_at == datetime(
        2026,
        7,
        9,
        13,
        30,
        tzinfo=legacy_pro_offer_service.MSK_TZ,
    )
    assert slot_by_key[legacy_pro_offer_service.B_SLOT_1].run_at == datetime(
        2026,
        7,
        9,
        13,
        30,
        tzinfo=legacy_pro_offer_service.MSK_TZ,
    )
    assert not slot_by_key["cold_solo_990_a_1"].is_one_off


def test_b_messages_use_990_price_and_group() -> None:
    first = legacy_pro_offer_service.get_message_for_slot(legacy_pro_offer_service.B_SLOT_1)
    last = legacy_pro_offer_service.get_message_for_slot(legacy_pro_offer_service.B_SLOT_4)

    assert first is not None
    assert first.groups == legacy_pro_offer_service.B_GROUPS
    assert first.button_text == "💎 Забрать год Pro за 990₽"
    assert "1 год на тарифе Pro (10 устр) за 990₽" in first.text
    assert "Это 82₽/мес" in first.text
    assert last is not None
    assert last.button_text == "💎 Забрать Pro за 990₽"
    assert "Год Pro за 990₽" in last.text


def test_interactive_service_latest_due_one_off_slot() -> None:
    now_msk = datetime(2026, 7, 18, 12, 0, tzinfo=legacy_pro_offer_service.MSK_TZ)

    assert interactive_notification_service._latest_due_one_off_slot_key(now_msk) in (
        legacy_pro_offer_service.AC_SLOT_2,
        legacy_pro_offer_service.B_SLOT_2,
    )
