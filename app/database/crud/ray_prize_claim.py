import logging
from datetime import datetime
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import RayPrizeClaim, RayPrizeClaimStatus

logger = logging.getLogger(__name__)


async def create_prize_claim(
    db: AsyncSession,
    *,
    user_id: int,
    prize_code: str,
    prize_title: str,
    cost_rays: int,
    spend_transaction_id: Optional[int] = None,
    contact: Optional[str] = None,
    commit: bool = True,
) -> RayPrizeClaim:
    claim = RayPrizeClaim(
        user_id=user_id,
        prize_code=prize_code,
        prize_title=prize_title,
        cost_rays=cost_rays,
        status=RayPrizeClaimStatus.PENDING.value,
        spend_transaction_id=spend_transaction_id,
        contact=contact,
    )
    db.add(claim)
    if commit:
        await db.commit()
        await db.refresh(claim)
    else:
        await db.flush()
    return claim


async def get_claim_by_id(db: AsyncSession, claim_id: int) -> Optional[RayPrizeClaim]:
    result = await db.execute(
        select(RayPrizeClaim).where(RayPrizeClaim.id == claim_id)
    )
    return result.scalar_one_or_none()


async def get_user_claims(
    db: AsyncSession,
    user_id: int,
    limit: int = 20,
) -> List[RayPrizeClaim]:
    result = await db.execute(
        select(RayPrizeClaim)
        .where(RayPrizeClaim.user_id == user_id)
        .order_by(RayPrizeClaim.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def mark_claim_completed(
    db: AsyncSession,
    claim: RayPrizeClaim,
    *,
    commit: bool = True,
) -> RayPrizeClaim:
    claim.status = RayPrizeClaimStatus.COMPLETED.value
    claim.completed_at = datetime.utcnow()
    claim.updated_at = datetime.utcnow()
    if commit:
        await db.commit()
        await db.refresh(claim)
    else:
        await db.flush()
    return claim


async def mark_claim_cancelled(
    db: AsyncSession,
    claim: RayPrizeClaim,
    *,
    refund_transaction_id: Optional[int] = None,
    commit: bool = True,
) -> RayPrizeClaim:
    claim.status = RayPrizeClaimStatus.CANCELLED.value
    claim.cancelled_at = datetime.utcnow()
    claim.updated_at = datetime.utcnow()
    if refund_transaction_id is not None:
        claim.refund_transaction_id = refund_transaction_id
    if commit:
        await db.commit()
        await db.refresh(claim)
    else:
        await db.flush()
    return claim
