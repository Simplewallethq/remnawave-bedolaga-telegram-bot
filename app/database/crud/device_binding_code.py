from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import DeviceBindingCode, Subscription

logger = logging.getLogger(__name__)

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 16
DEFAULT_TTL_SECONDS = 24 * 60 * 60

CONSUME_OK = "ok"
CONSUME_NOT_FOUND = "not_found"
CONSUME_EXPIRED = "expired"
CONSUME_USED = "used"


def _generate_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


async def _find_active_code(
    db: AsyncSession, subscription_id: int, now: datetime
) -> DeviceBindingCode | None:
    result = await db.execute(
        select(DeviceBindingCode)
        .where(
            DeviceBindingCode.subscription_id == subscription_id,
            DeviceBindingCode.used_at.is_(None),
            DeviceBindingCode.expires_at > now,
        )
        .order_by(DeviceBindingCode.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_or_create_binding_code(
    db: AsyncSession,
    subscription_id: int,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> DeviceBindingCode:
    """Return the current active code for the subscription, or create a fresh one.

    Reusing an in-flight code is intentional: a user double-clicking the button
    shouldn't get a different code each time.
    """
    now = datetime.utcnow()
    existing = await _find_active_code(db, subscription_id, now)
    if existing is not None:
        return existing

    expires_at = now + timedelta(seconds=ttl_seconds)
    for _ in range(5):
        code = _generate_code()
        record = DeviceBindingCode(
            subscription_id=subscription_id,
            code=code,
            expires_at=expires_at,
        )
        db.add(record)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            continue
        await db.refresh(record)
        return record

    raise RuntimeError("Failed to generate a unique binding code after 5 attempts")


async def consume_binding_code(
    db: AsyncSession, code: str, device_id: str
) -> tuple[Subscription | None, str]:
    """Atomically validate and mark a code as used.

    Returns (subscription, status):
        - (subscription, CONSUME_OK) on success
        - (None, CONSUME_NOT_FOUND) if no such code
        - (None, CONSUME_EXPIRED) if expired
        - (None, CONSUME_USED) if already used
    """
    result = await db.execute(
        select(DeviceBindingCode).where(DeviceBindingCode.code == code).with_for_update()
    )
    record = result.scalar_one_or_none()
    if record is None:
        return None, CONSUME_NOT_FOUND

    now = datetime.utcnow()
    if record.used_at is not None:
        return None, CONSUME_USED
    if record.expires_at <= now:
        return None, CONSUME_EXPIRED

    record.used_at = now
    record.used_device_id = device_id

    sub_result = await db.execute(
        select(Subscription).where(Subscription.id == record.subscription_id)
    )
    subscription = sub_result.scalar_one_or_none()

    await db.commit()
    if subscription is not None:
        await db.refresh(subscription)
    return subscription, CONSUME_OK


async def purge_expired_codes(db: AsyncSession, older_than_days: int = 7) -> int:
    """Delete codes that expired more than `older_than_days` days ago."""
    cutoff = datetime.utcnow() - timedelta(days=older_than_days)
    result = await db.execute(
        select(DeviceBindingCode).where(DeviceBindingCode.expires_at < cutoff)
    )
    rows = result.scalars().all()
    for row in rows:
        await db.delete(row)
    if rows:
        await db.commit()
    return len(rows)
