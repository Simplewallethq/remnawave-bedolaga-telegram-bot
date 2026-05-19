"""CRUD operations for partner VIP-link redemptions."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import PartnerLinkRedemption


logger = logging.getLogger(__name__)


async def try_insert_redemption(
    db: AsyncSession,
    *,
    jti: str,
    user_id: int,
    sub_until: datetime,
    subscription_id: Optional[int] = None,
    commit: bool = True,
) -> bool:
    """Atomically insert a redemption row. Returns True if inserted, False if jti
    was already redeemed (UNIQUE conflict).

    Uses Postgres ``INSERT ... ON CONFLICT (jti) DO NOTHING RETURNING id`` for
    race-free one-time-use enforcement.
    """
    stmt = (
        pg_insert(PartnerLinkRedemption.__table__)
        .values(
            jti=jti,
            user_id=user_id,
            subscription_id=subscription_id,
            sub_until=sub_until,
        )
        .on_conflict_do_nothing(index_elements=["jti"])
        .returning(PartnerLinkRedemption.__table__.c.id)
    )
    result = await db.execute(stmt)
    inserted_id = result.scalar()

    if inserted_id is None:
        return False

    if commit:
        await db.commit()
    else:
        await db.flush()
    return True


async def attach_subscription_id(
    db: AsyncSession,
    *,
    jti: str,
    subscription_id: int,
    commit: bool = True,
) -> None:
    """Backfill subscription_id on a redemption row created before the subscription existed.

    Used when the redemption is inserted in the same transaction as a new
    subscription — the subscription's id is unknown until flush.
    """
    row = await db.execute(
        select(PartnerLinkRedemption).where(PartnerLinkRedemption.jti == jti)
    )
    redemption = row.scalar_one_or_none()
    if redemption is None:
        return
    redemption.subscription_id = subscription_id
    if commit:
        await db.commit()
    else:
        await db.flush()


async def get_redemption_by_jti(
    db: AsyncSession, jti: str
) -> Optional[PartnerLinkRedemption]:
    result = await db.execute(
        select(PartnerLinkRedemption).where(PartnerLinkRedemption.jti == jti)
    )
    return result.scalar_one_or_none()
