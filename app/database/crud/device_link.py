from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models import DeviceLink, Subscription

logger = logging.getLogger(__name__)


async def get_device_link(db: AsyncSession, device_id: str) -> DeviceLink | None:
    """Get a device link by device_id. Returns None if not found."""
    result = await db.execute(
        select(DeviceLink).where(DeviceLink.device_id == device_id)
    )
    return result.scalar_one_or_none()


async def count_device_links(db: AsyncSession, subscription_id: int) -> int:
    """Count how many devices are linked to a subscription. Per D-03."""
    result = await db.execute(
        select(func.count()).select_from(DeviceLink).where(
            DeviceLink.subscription_id == subscription_id
        )
    )
    return result.scalar_one()


async def create_device_link(db: AsyncSession, subscription_id: int, device_id: str) -> DeviceLink:
    """Create a new device link. Caller must check uniqueness/limits first."""
    link = DeviceLink(subscription_id=subscription_id, device_id=device_id)
    db.add(link)
    await db.commit()
    await db.refresh(link)
    return link


async def revoke_device_links(db: AsyncSession, device_ids: list[str]) -> int:
    """Пометить привязки как отозванные. Строки остаются — по ним видно, выдавался ли триал.

    Возвращает число помеченных (уже отозванные не считаются).
    """
    if not device_ids:
        return 0
    result = await db.execute(
        update(DeviceLink)
        .where(DeviceLink.device_id.in_(device_ids), DeviceLink.revoked_at.is_(None))
        .values(revoked_at=datetime.utcnow())
    )
    await db.commit()
    return result.rowcount or 0


async def revoke_all_device_links(db: AsyncSession, subscription_id: int) -> int:
    """То же для всех устройств подписки — сброс из ЛК/бота."""
    result = await db.execute(
        update(DeviceLink)
        .where(
            DeviceLink.subscription_id == subscription_id,
            DeviceLink.revoked_at.is_(None),
        )
        .values(revoked_at=datetime.utcnow())
    )
    await db.commit()
    return result.rowcount or 0


async def rebind_device_link(
    db: AsyncSession, link: DeviceLink, new_subscription_id: int
) -> DeviceLink:
    """Move an existing device link to a different subscription via in-place UPDATE.

    Avoids delete+insert so the device_id unique index is never momentarily empty
    or duplicated. Caller must enforce the new subscription's device_limit before
    invoking.
    """
    link.subscription_id = new_subscription_id
    link.linked_at = datetime.utcnow()
    link.revoked_at = None
    await db.commit()
    await db.refresh(link)
    return link


async def reactivate_device_link(db: AsyncSession, link: DeviceLink) -> DeviceLink:
    """Снять отзыв: устройство подключили заново. Без этого отозванное однажды
    устройство осталось бы отозванным навсегда."""
    if link.revoked_at is not None:
        link.revoked_at = None
        link.linked_at = datetime.utcnow()
        await db.commit()
        await db.refresh(link)
    return link


async def get_subscription_by_device_id(db: AsyncSession, device_id: str) -> Subscription | None:
    """Get subscription associated with a device_id via JOIN. Returns None if device not found."""
    result = await db.execute(
        select(Subscription)
        .options(selectinload(Subscription.user))
        .join(DeviceLink, DeviceLink.subscription_id == Subscription.id)
        .where(DeviceLink.device_id == device_id)
    )
    return result.scalar_one_or_none()
