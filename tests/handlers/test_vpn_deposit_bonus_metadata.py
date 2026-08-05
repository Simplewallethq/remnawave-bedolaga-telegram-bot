from types import SimpleNamespace

from app.handlers.balance.vpn_deposit_bonus import (
    build_vpn_deposit_bonus_metadata,
    merge_vpn_deposit_bonus_metadata,
    should_apply_vpn_deposit_bonus,
    should_bypass_minimum,
)
from app.services.vpn_deposit_bonus_service import vpn_deposit_bonus_service


INVOICE = vpn_deposit_bonus_service.INVOICE_AMOUNT_KOPEKS


def _db_user(user_id: int = 254334) -> SimpleNamespace:
    return SimpleNamespace(id=user_id, telegram_id=user_id, language="ru")


def _state(user_id: int = 254334, expires_at: str | None = None) -> dict:
    metadata = vpn_deposit_bonus_service.build_payment_metadata(user_id)
    if expires_at:
        metadata["expires_at"] = expires_at
    return {
        "topup_purpose": vpn_deposit_bonus_service.PURPOSE,
        "vpn_deposit_bonus_metadata": metadata,
    }


def test_campaign_metadata_applied_for_invoice_amount() -> None:
    db_user = _db_user()

    metadata = build_vpn_deposit_bonus_metadata(db_user, _state(), amount_kopeks=INVOICE)

    assert metadata is not None
    assert metadata["purpose"] == vpn_deposit_bonus_service.PURPOSE
    assert metadata["campaign_key"] == vpn_deposit_bonus_service.campaign_key(db_user.id)
    assert should_bypass_minimum(_state(), INVOICE)
    assert should_apply_vpn_deposit_bonus(_state(), INVOICE)


def test_campaign_metadata_not_applied_to_other_amounts() -> None:
    db_user = _db_user()
    state_data = _state()

    assert build_vpn_deposit_bonus_metadata(db_user, state_data, amount_kopeks=99_000) is None
    assert not should_bypass_minimum(state_data, 99_000)
    assert not should_apply_vpn_deposit_bonus(state_data, 99_000)

    merged = merge_vpn_deposit_bonus_metadata(
        {"purpose": "balance_topup"},
        db_user,
        state_data,
        amount_kopeks=99_000,
    )
    assert merged == {"purpose": "balance_topup"}


def test_campaign_metadata_not_applied_after_expiry() -> None:
    db_user = _db_user()
    state_data = _state(expires_at="2020-01-01T20:59:59")

    assert build_vpn_deposit_bonus_metadata(db_user, state_data, amount_kopeks=INVOICE) is None


def test_metadata_not_applied_without_campaign_state() -> None:
    assert build_vpn_deposit_bonus_metadata(_db_user(), {}, amount_kopeks=INVOICE) is None
    assert build_vpn_deposit_bonus_metadata(_db_user(), None, amount_kopeks=INVOICE) is None
