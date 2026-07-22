from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Security, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.crud.device_binding_code import (
    CONSUME_EXPIRED,
    CONSUME_NOT_FOUND,
    CONSUME_OK,
    CONSUME_USED,
    consume_binding_code,
)
from app.database.crud.device_link import (
    count_device_links,
    create_device_link,
    get_device_link,
    get_subscription_by_device_id,
    rebind_device_link,
)
from app.database.crud.share_token import (
    ACTIVATE_DEAD,
    ACTIVATE_LIMIT,
    ACTIVATE_NOT_FOUND,
    ACTIVATE_OK,
    ACTIVATE_SUB_INACTIVE,
    activate_share_code,
)

from app.database.models import Subscription
from app.utils.install_referrer import apply_app_name, apply_install_referrer

from ..dependencies import get_db_session, require_api_token
from ..schemas.devices import BindByCodeRequest, DeviceLinkRequest, DeviceLinkResponse
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
    # Этот эндпоинт вызывает только teleVpn при поллинге статуса устройства,
    # поэтому успешный ответ означает вход в приложение через Telegram
    # (deep-link или код привязки). Email-логин ставит флаг в auth_otp.py.
    user = subscription.user
    if user is not None and not user.has_used_mobile_app:
        user.has_used_mobile_app = True
        await db.commit()
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
        422: {"description": "Device limit exceeded on the target subscription"},
    },
)
async def link_device(
    device_id: str,
    payload: DeviceLinkRequest,
    _=Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> DeviceLinkResponse:
    """BOT-06: Bind device_id to a subscription. Enforces device limit per D-03.

    If the device is already linked to a different subscription it is moved
    (re-bound) to the new one — there is no 409. The target subscription's
    device_limit is still enforced against the link count *excluding* the
    incoming device (since the link is moved in place, not duplicated).
    """
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

    existing = await get_device_link(db, device_id)
    if existing and existing.subscription_id == subscription.id:
        # Idempotent: already linked to the same subscription.
        return DeviceLinkResponse(
            device_id=device_id,
            subscription_id=subscription.id,
            linked_at=existing.linked_at,
        )

    # Enforce device limit on the TARGET subscription. The incoming device is
    # never on the target yet (otherwise we'd have returned above), so the raw
    # count is the right comparison even for rebinds.
    count = await count_device_links(db, subscription.id)
    limit = subscription.device_limit or 1
    if count >= limit:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Device limit exceeded ({count}/{limit})",
        )

    if existing is not None:
        logger.info(
            "Rebinding device %s from subscription %s to %s",
            device_id, existing.subscription_id, subscription.id,
        )
        link = await rebind_device_link(db, existing, subscription.id)
    else:
        link = await create_device_link(db, subscription.id, device_id)

    return DeviceLinkResponse(
        device_id=device_id,
        subscription_id=subscription.id,
        linked_at=link.linked_at,
    )


@router.post(
    "/bind-by-code",
    response_model=SubscriptionResponse,
    status_code=status.HTTP_200_OK,
    summary="Bind device to a subscription using a one-time binding code",
    responses={
        404: {"description": "Code not found, expired, or already used"},
        422: {"description": "Device limit exceeded on the target subscription"},
    },
)
async def bind_device_by_code(
    payload: BindByCodeRequest,
    _=Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> SubscriptionResponse:
    """Validate a binding code and bind the supplied device_id to the code's subscription.

    Flow:
    1. Atomically consume the code (404 if missing/expired/used).
    2. If the device is already linked to the SAME subscription — idempotent success.
    3. If the device is linked to a DIFFERENT subscription — move it (rebind).
    4. If the target subscription is at its device_limit — 422.
    5. Otherwise create the DeviceLink and return the subscription payload.

    A valid binding code is treated as the user's explicit consent to attach
    this device to the code's subscription, so a pre-existing link to another
    subscription is silently replaced rather than rejected with 409.
    """
    existing_link = await get_device_link(db, payload.device_id)

    subscription, consume_status = await consume_binding_code(
        db, payload.code, payload.device_id
    )

    if consume_status == CONSUME_NOT_FOUND:
        # Не личный код привязки — пробуем share-код страницы «Поделиться доступом»
        # (тот же формат, многоразовый, со счётчиком активаций).
        return await _bind_via_share_code(db, payload)
    if consume_status == CONSUME_EXPIRED:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Code expired")
    if consume_status == CONSUME_USED:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Code already used")
    if consume_status != CONSUME_OK or subscription is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Code not found")

    # Re-fetch the subscription with the device_links relationship hydrated.
    sub_result = await db.execute(
        select(Subscription)
        .options(selectinload(Subscription.user))
        .where(Subscription.id == subscription.id)
    )
    subscription = sub_result.scalar_one_or_none()
    if subscription is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Subscription not found",
        )

    # Атрибуция установки: личный код вводит владелец подписки, поэтому
    # Install Referrer применяем к нему. Share-код (_bind_via_share_code)
    # пропускаем — там устройство привязывает не владелец.
    if subscription.user is not None:
        changed = apply_install_referrer(
            subscription.user, payload.install_referrer
        )
        if apply_app_name(subscription.user, payload.app_name):
            changed = True
        if changed:
            await db.commit()

    if existing_link is not None and existing_link.subscription_id == subscription.id:
        response = _serialize_subscription(subscription)
        response.connected_devices = await count_device_links(db, subscription.id)
        return response

    # Enforce device limit on the TARGET subscription. The incoming device is
    # not on the target yet (handled above), so the raw count is correct for
    # both fresh binds and rebinds.
    count = await count_device_links(db, subscription.id)
    limit = subscription.device_limit or 1
    if count >= limit:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Device limit exceeded ({count}/{limit})",
        )

    if existing_link is not None:
        logger.info(
            "Rebinding device %s from subscription %s to %s via binding code",
            payload.device_id, existing_link.subscription_id, subscription.id,
        )
        await rebind_device_link(db, existing_link, subscription.id)
    else:
        await create_device_link(db, subscription.id, payload.device_id)

    response = _serialize_subscription(subscription)
    response.connected_devices = await count_device_links(db, subscription.id)
    return response


async def _bind_via_share_code(
    db: AsyncSession, payload: BindByCodeRequest
) -> SubscriptionResponse:
    """Привязка устройства по share-коду страницы «Поделиться доступом».

    Отличия от личного кода: код многоразовый (до max_activations), на лимите
    устройств активация НЕ расходуется, повторная привязка того же устройства
    идемпотентна. Ошибки отдаём в тех же формулировках, что личный код, —
    приложение их уже понимает.
    """
    subscription, share_status, extra = await activate_share_code(
        db, payload.code, payload.device_id
    )

    if share_status in (ACTIVATE_NOT_FOUND, ACTIVATE_DEAD):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Code not found")
    if share_status == ACTIVATE_SUB_INACTIVE:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Code expired")
    if share_status == ACTIVATE_LIMIT:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Device limit exceeded ({extra['count']}/{extra['limit']})",
        )
    if share_status != ACTIVATE_OK or subscription is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Code not found")

    # Перечитываем подписку с прогретым user для сериализации.
    sub_result = await db.execute(
        select(Subscription)
        .options(selectinload(Subscription.user))
        .where(Subscription.id == subscription.id)
    )
    subscription = sub_result.scalar_one_or_none()
    if subscription is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Subscription not found",
        )

    response = _serialize_subscription(subscription)
    response.connected_devices = await count_device_links(db, subscription.id)
    return response
