from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import CabinetNotification


logger = logging.getLogger(__name__)


async def create_cabinet_notification(
    db: AsyncSession,
    *,
    user_id: int,
    type: str,
    title: Optional[str] = None,
    body: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> CabinetNotification:
    notification = CabinetNotification(
        user_id=user_id,
        type=type,
        title=title,
        body=body,
        payload=payload or None,
    )
    db.add(notification)
    await db.commit()
    await db.refresh(notification)
    return notification


async def bulk_create_notifications(
    db: AsyncSession,
    user_ids: Iterable[int],
    *,
    type: str,
    title: Optional[str] = None,
    body: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
    chunk_size: int = 1000,
) -> int:
    created = 0
    ids = list(dict.fromkeys(user_ids))
    for start in range(0, len(ids), chunk_size):
        chunk = ids[start:start + chunk_size]
        db.add_all(
            CabinetNotification(
                user_id=user_id,
                type=type,
                title=title,
                body=body,
                payload=payload or None,
            )
            for user_id in chunk
        )
        await db.commit()
        created += len(chunk)
    return created


async def list_cabinet_notifications(
    db: AsyncSession,
    user_id: int,
    *,
    limit: int = 50,
    offset: int = 0,
    unread_only: bool = False,
    since_id: Optional[int] = None,
) -> Tuple[List[CabinetNotification], int]:
    filters = [CabinetNotification.user_id == user_id]
    if unread_only:
        filters.append(CabinetNotification.is_read == False)  # noqa: E712
    if since_id:
        filters.append(CabinetNotification.id > since_id)

    base_query = select(CabinetNotification).where(and_(*filters))

    total_query = base_query.with_only_columns(func.count()).order_by(None)
    total = await db.scalar(total_query) or 0

    result = await db.execute(
        base_query.order_by(CabinetNotification.id.desc()).offset(offset).limit(limit)
    )
    return list(result.scalars().all()), int(total)


async def get_missed_notifications(
    db: AsyncSession,
    user_id: int,
    *,
    since_id: int,
    limit: int = 50,
) -> List[CabinetNotification]:
    """Пропущенные события для backfill при переподключении SSE (по возрастанию id)."""
    result = await db.execute(
        select(CabinetNotification)
        .where(
            and_(
                CabinetNotification.user_id == user_id,
                CabinetNotification.id > since_id,
            )
        )
        .order_by(CabinetNotification.id.asc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def get_unread_count(db: AsyncSession, user_id: int) -> int:
    count = await db.scalar(
        select(func.count())
        .select_from(CabinetNotification)
        .where(
            and_(
                CabinetNotification.user_id == user_id,
                CabinetNotification.is_read == False,  # noqa: E712
            )
        )
    )
    return int(count or 0)


async def mark_notification_read(
    db: AsyncSession, user_id: int, notification_id: int
) -> bool:
    result = await db.execute(
        update(CabinetNotification)
        .where(
            and_(
                CabinetNotification.id == notification_id,
                CabinetNotification.user_id == user_id,
                CabinetNotification.is_read == False,  # noqa: E712
            )
        )
        .values(is_read=True, read_at=datetime.utcnow())
    )
    await db.commit()
    return bool(result.rowcount)


async def mark_all_read(db: AsyncSession, user_id: int) -> int:
    result = await db.execute(
        update(CabinetNotification)
        .where(
            and_(
                CabinetNotification.user_id == user_id,
                CabinetNotification.is_read == False,  # noqa: E712
            )
        )
        .values(is_read=True, read_at=datetime.utcnow())
    )
    await db.commit()
    return int(result.rowcount or 0)


async def cleanup_old_notifications(
    db: AsyncSession,
    *,
    retention_days: int,
    max_per_user: int,
) -> int:
    deleted = 0

    if retention_days > 0:
        threshold = datetime.utcnow() - timedelta(days=retention_days)
        result = await db.execute(
            delete(CabinetNotification).where(CabinetNotification.created_at < threshold)
        )
        deleted += int(result.rowcount or 0)

    if max_per_user > 0:
        overflow_users = await db.execute(
            select(CabinetNotification.user_id)
            .group_by(CabinetNotification.user_id)
            .having(func.count() > max_per_user)
        )
        for (user_id,) in overflow_users.all():
            keep_ids = select(CabinetNotification.id).where(
                CabinetNotification.user_id == user_id
            ).order_by(CabinetNotification.id.desc()).limit(max_per_user)
            result = await db.execute(
                delete(CabinetNotification).where(
                    and_(
                        CabinetNotification.user_id == user_id,
                        CabinetNotification.id.not_in(keep_ids.scalar_subquery()),
                    )
                )
            )
            deleted += int(result.rowcount or 0)

    await db.commit()
    return deleted
