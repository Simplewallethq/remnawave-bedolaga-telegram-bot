from __future__ import annotations

from typing import Optional

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import AdCampaignVisit, User


async def record_ad_visit(
    db: AsyncSession,
    telegram_id: int,
    raw_payload: str,
    source: str,
    campaign_id: str,
    is_new_user: bool,
    user_id: Optional[int] = None,
) -> AdCampaignVisit:
    visit = AdCampaignVisit(
        telegram_id=telegram_id,
        user_id=user_id,
        raw_payload=raw_payload,
        source=source,
        campaign_id=campaign_id,
        is_new_user=is_new_user,
    )
    db.add(visit)
    await db.flush()
    return visit


async def link_visit_to_user(
    db: AsyncSession,
    telegram_id: int,
    user_id: int,
) -> None:
    await db.execute(
        update(AdCampaignVisit)
        .where(
            AdCampaignVisit.telegram_id == telegram_id,
            AdCampaignVisit.user_id.is_(None),
        )
        .values(user_id=user_id)
    )


async def set_user_attribution(
    db: AsyncSession,
    user_id: int,
    raw_payload: str,
    source: str,
    campaign_id: str,
) -> None:
    """Write first-touch attribution to the user. No-op if already set."""
    await db.execute(
        update(User)
        .where(
            User.id == user_id,
            User.attribution_campaign_id.is_(None),
        )
        .values(
            raw_start_payload=raw_payload,
            attribution_source=source,
            attribution_campaign_id=campaign_id,
        )
    )


async def get_ad_campaign_stats(db: AsyncSession, campaign_id: str) -> dict:
    arrived_result = await db.execute(
        select(func.count()).where(AdCampaignVisit.campaign_id == campaign_id)
    )
    arrived = arrived_result.scalar() or 0

    registered_result = await db.execute(
        select(func.count()).where(User.attribution_campaign_id == campaign_id)
    )
    registered = registered_result.scalar() or 0

    paid_result = await db.execute(
        select(func.count()).where(
            User.attribution_campaign_id == campaign_id,
            User.has_had_paid_subscription.is_(True),
        )
    )
    paid = paid_result.scalar() or 0

    reg_conversion = round(registered / arrived * 100, 1) if arrived else 0.0
    pay_conversion = round(paid / registered * 100, 1) if registered else 0.0

    return {
        "campaign_id": campaign_id,
        "arrived": arrived,
        "registered": registered,
        "paid": paid,
        "reg_conversion_pct": reg_conversion,
        "pay_conversion_pct": pay_conversion,
    }
