from __future__ import annotations

from typing import Any

from app.database.models import User
from app.services.vpn_deposit_bonus_service import vpn_deposit_bonus_service


def is_vpn_deposit_bonus_state(state_data: dict[str, Any] | None) -> bool:
    return bool(
        state_data
        and state_data.get("topup_purpose") == vpn_deposit_bonus_service.PURPOSE
    )


def should_bypass_minimum(state_data: dict[str, Any] | None, amount_kopeks: int) -> bool:
    return (
        is_vpn_deposit_bonus_state(state_data)
        and amount_kopeks == vpn_deposit_bonus_service.INVOICE_AMOUNT_KOPEKS
    )


def build_vpn_deposit_bonus_metadata(
    db_user: User,
    state_data: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not is_vpn_deposit_bonus_state(state_data):
        return None
    if not vpn_deposit_bonus_service.is_debug_user_allowed(db_user):
        return None
    metadata = dict(state_data.get("vpn_deposit_bonus_metadata") or {})
    metadata.update(vpn_deposit_bonus_service.build_payment_metadata(db_user.id))
    return metadata


def merge_vpn_deposit_bonus_metadata(
    metadata: dict[str, Any],
    db_user: User,
    state_data: dict[str, Any] | None,
) -> dict[str, Any]:
    bonus_metadata = build_vpn_deposit_bonus_metadata(db_user, state_data)
    if bonus_metadata:
        metadata.update(bonus_metadata)
    return metadata
