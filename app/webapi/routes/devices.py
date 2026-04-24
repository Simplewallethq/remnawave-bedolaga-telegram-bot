from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Security, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.device_link import (
    count_device_links,
    create_device_link,
    get_device_link,
    get_subscription_by_device_id,
)

from app.database.models import Subscription

from ..dependencies import get_db_session, require_api_token
from ..schemas.devices import DeviceLinkRequest, DeviceLinkResponse
from ..schemas.subscriptions import SubscriptionResponse
from .subscriptions import _serialize_subscription

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get(
    "/{device_id}/subscription",
    response_model=SubscriptionResponse,
    summary="Get subscription by device_id",
    responses={404: {"description": "Device not found"}},
)
async def get_device_subscription(
    device_id: str,
    _=Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> SubscriptionResponse:
    """BOT-05: Lookup subscription by device_id. Returns 404 if device not found."""
    subscription = await get_subscription_by_device_id(db, device_id)
    if not subscription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Device not found",
        )
    response = _serialize_subscription(subscription)
    response.connected_devices = await count_device_links(db, subscription.id)
    return response


@router.post(
    "/{device_id}/link",
    response_model=DeviceLinkResponse,
    status_code=status.HTTP_200_OK,
    summary="Link device to subscription",
    responses={
        404: {"description": "Subscription not found"},
        409: {"description": "Device already linked to a different subscription"},
        422: {"description": "Device limit exceeded"},
    },
)
async def link_device(
    device_id: str,
    payload: DeviceLinkRequest,
    _=Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> DeviceLinkResponse:
    """BOT-06: Bind device_id to a subscription. Enforces device limit per D-03."""
    # Fetch subscription
    result = await db.execute(
        select(Subscription).where(Subscription.id == payload.subscription_id)
    )
    subscription = result.scalar_one_or_none()
    if not subscription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Subscription not found",
        )

    # Check if device already linked
    existing = await get_device_link(db, device_id)
    if existing:
        if existing.subscription_id == subscription.id:
            # Idempotent: already linked to same subscription
            return DeviceLinkResponse(
                device_id=device_id,
                subscription_id=subscription.id,
                linked_at=existing.linked_at,
            )
        # Linked to different subscription
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Device already linked to a different subscription",
        )

    # Check device limit per D-03
    count = await count_device_links(db, subscription.id)
    limit = subscription.device_limit or 1
    if count >= limit:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Device limit exceeded ({count}/{limit})",
        )

    # Create link
    link = await create_device_link(db, subscription.id, device_id)
    return DeviceLinkResponse(
        device_id=device_id,
        subscription_id=subscription.id,
        linked_at=link.linked_at,
    )
