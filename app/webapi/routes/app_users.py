"""Внутренний API подписки по bot user_id для приложения teleVpn (email/OTP-путь).

Монтируется под /api/users и защищён X-API-Key (require_api_token), как
/api/devices и /api/auth. У email/OTP-пользователя нет привязанного устройства,
поэтому teleVpn получает срок и статус подписки (источник правды — бот) по
внутреннему user_id, который он замиррорил при OTP-логине.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Security, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.cabinet_notification import (
    get_unread_count,
    list_cabinet_notifications,
    mark_all_read,
    mark_notification_read,
)
from app.database.crud.device_link import count_device_links
from app.database.crud.subscription import get_subscription_by_user_id
from app.database.crud.user import get_user_by_id
from app.database.models import CabinetNotification, User

from ..dependencies import get_db_session, require_api_token
from ..schemas.notifications import (
    NotificationItem,
    NotificationListResponse,
    NotificationMarkAllReadResponse,
    NotificationMarkReadResponse,
    NotificationUnreadCountResponse,
)
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


async def _require_user(db: AsyncSession, user_id: int) -> User:
    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    return user


def _serialize_notification(notification: CabinetNotification) -> NotificationItem:
    return NotificationItem(
        id=notification.id,
        type=notification.type,
        title=notification.title,
        body=notification.body,
        payload=notification.payload,
        is_read=notification.is_read,
        created_at=notification.created_at,
    )


@router.get(
    "/{user_id}/notifications",
    response_model=NotificationListResponse,
    summary="List cabinet notifications by bot user_id",
    responses={404: {"description": "User not found"}},
)
async def get_user_notifications(
    user_id: int,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    unread_only: bool = Query(False),
    _=Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> NotificationListResponse:
    """Лента cabinet-уведомлений пользователя для приложений teleVpn."""
    user = await _require_user(db, user_id)
    items, total = await list_cabinet_notifications(
        db,
        user.id,
        limit=limit,
        offset=offset,
        unread_only=unread_only,
    )
    unread_count = await get_unread_count(db, user.id)
    return NotificationListResponse(
        items=[_serialize_notification(item) for item in items],
        total=total,
        unread_count=unread_count,
    )


@router.get(
    "/{user_id}/notifications/unread-count",
    response_model=NotificationUnreadCountResponse,
    summary="Get unread notifications count by bot user_id",
    responses={404: {"description": "User not found"}},
)
async def get_user_notifications_unread_count(
    user_id: int,
    _=Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> NotificationUnreadCountResponse:
    user = await _require_user(db, user_id)
    unread_count = await get_unread_count(db, user.id)
    return NotificationUnreadCountResponse(unread_count=unread_count)


@router.post(
    "/{user_id}/notifications/read-all",
    response_model=NotificationMarkAllReadResponse,
    summary="Mark all notifications as read by bot user_id",
    responses={404: {"description": "User not found"}},
)
async def mark_user_notifications_read_all(
    user_id: int,
    _=Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> NotificationMarkAllReadResponse:
    user = await _require_user(db, user_id)
    marked = await mark_all_read(db, user.id)
    return NotificationMarkAllReadResponse(marked=marked)


@router.post(
    "/{user_id}/notifications/{notification_id}/read",
    response_model=NotificationMarkReadResponse,
    summary="Mark a notification as read by bot user_id",
    responses={404: {"description": "User or notification not found"}},
)
async def mark_user_notification_read(
    user_id: int,
    notification_id: int,
    _=Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> NotificationMarkReadResponse:
    user = await _require_user(db, user_id)
    updated = await mark_notification_read(db, user.id, notification_id)
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Notification not found",
        )
    unread_count = await get_unread_count(db, user.id)
    return NotificationMarkReadResponse(unread_count=unread_count)
