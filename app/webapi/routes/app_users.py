"""Внутренний API подписки по bot user_id для приложения teleVpn (email/OTP-путь).

Монтируется под /api/users и защищён X-API-Key (require_api_token), как
/api/devices и /api/auth. У email/OTP-пользователя нет привязанного устройства,
поэтому teleVpn получает срок и статус подписки (источник правды — бот) по
внутреннему user_id, который он замиррорил при OTP-логине.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Security, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.device_link import count_device_links
from app.database.crud.subscription import get_subscription_by_user_id

from ..dependencies import get_db_session, require_api_token
from ..schemas.subscriptions import SubscriptionResponse
from .subscriptions import _serialize_subscription

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get(
    "/{user_id}/subscription",
    response_model=SubscriptionResponse,
    summary="Get current subscription by bot user_id",
    responses={404: {"description": "Subscription not found"}},
)
async def get_user_subscription(
    user_id: int,
    _=Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> SubscriptionResponse:
    """Вернуть актуальную подписку пользователя по его внутреннему user_id.

    Отдаёт живые end_date/actual_status — источник правды для срока подписки
    (teleVpn использует это вместо ранее захардкоженного TTL). 404, если у
    пользователя нет подписки.
    """
    subscription = await get_subscription_by_user_id(db, user_id)
    if not subscription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Subscription not found",
        )
    response = _serialize_subscription(subscription)
    response.connected_devices = await count_device_links(db, subscription.id)
    return response
