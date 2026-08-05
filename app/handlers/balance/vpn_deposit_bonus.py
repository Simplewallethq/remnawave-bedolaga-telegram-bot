from __future__ import annotations

import logging
from typing import Any

from app.database.models import User
from app.services.vpn_deposit_bonus_service import vpn_deposit_bonus_service


logger = logging.getLogger(__name__)


def is_vpn_deposit_bonus_state(state_data: dict[str, Any] | None) -> bool:
    return bool(
        state_data
        and state_data.get("topup_purpose") == vpn_deposit_bonus_service.PURPOSE
    )


def should_bypass_minimum(state_data: dict[str, Any] | None, amount_kopeks: int) -> bool:
    return (
        is_vpn_deposit_bonus_state(state_data)
        and vpn_deposit_bonus_service.is_campaign_amount(amount_kopeks)
    )


def should_apply_vpn_deposit_bonus(
    state_data: dict[str, Any] | None,
    amount_kopeks: int,
) -> bool:
    return should_bypass_minimum(state_data, amount_kopeks)


def build_vpn_deposit_bonus_metadata(
    db_user: User,
    state_data: dict[str, Any] | None,
    *,
    amount_kopeks: int,
) -> dict[str, Any] | None:
    if not is_vpn_deposit_bonus_state(state_data):
        return None
    if not vpn_deposit_bonus_service.is_debug_user_allowed(db_user):
        return None
    if not vpn_deposit_bonus_service.is_campaign_amount(amount_kopeks):
        # Состояние кампании осталось от прошлого шага, а сумма счёта другая:
        # обычное пополнение, метку кампании не вешаем.
        logger.warning(
            "Пользователь %s создаёт счёт на %s копеек в состоянии %s — метка кампании не применяется",
            getattr(db_user, "id", None),
            amount_kopeks,
            vpn_deposit_bonus_service.PURPOSE,
        )
        return None

    metadata = dict(state_data.get("vpn_deposit_bonus_metadata") or {})
    if vpn_deposit_bonus_service.is_metadata_expired(metadata):
        logger.warning(
            "Предложение %s для пользователя %s истекло (%s) — метка кампании не применяется",
            vpn_deposit_bonus_service.PURPOSE,
            getattr(db_user, "id", None),
            metadata.get("expires_at"),
        )
        return None

    metadata.update(vpn_deposit_bonus_service.build_payment_metadata(db_user.id))
    return metadata


def merge_vpn_deposit_bonus_metadata(
    metadata: dict[str, Any],
    db_user: User,
    state_data: dict[str, Any] | None,
    *,
    amount_kopeks: int,
) -> dict[str, Any]:
    bonus_metadata = build_vpn_deposit_bonus_metadata(
        db_user,
        state_data,
        amount_kopeks=amount_kopeks,
    )
    if bonus_metadata:
        metadata.update(bonus_metadata)
    return metadata
