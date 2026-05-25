"""Lookups and upgrade-delta math for the tiered subscription plans (App/Solo/Plus/Pro).

Legacy à-la-carte subscriptions (Subscription.plan_id IS NULL) have no entry here —
callers should branch on Subscription.is_legacy and use the old pricing pipeline for them.
"""

import logging
from datetime import datetime
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models import Subscription, SubscriptionPlan, SubscriptionPlanPrice

logger = logging.getLogger(__name__)


SUPPORTED_PERIOD_DAYS: List[int] = [30, 90, 180, 360, 720]


async def list_active_plans(db: AsyncSession) -> List[SubscriptionPlan]:
    """Active plans ordered by sort_order, with prices eagerly loaded."""
    result = await db.execute(
        select(SubscriptionPlan)
        .where(SubscriptionPlan.is_active.is_(True))
        .order_by(SubscriptionPlan.sort_order, SubscriptionPlan.id)
        .options(selectinload(SubscriptionPlan.prices))
    )
    return list(result.scalars().all())


async def get_plan_by_code(db: AsyncSession, code: str) -> Optional[SubscriptionPlan]:
    result = await db.execute(
        select(SubscriptionPlan)
        .where(SubscriptionPlan.code == code)
        .options(selectinload(SubscriptionPlan.prices))
    )
    return result.scalar_one_or_none()


async def get_plan_by_id(db: AsyncSession, plan_id: int) -> Optional[SubscriptionPlan]:
    result = await db.execute(
        select(SubscriptionPlan)
        .where(SubscriptionPlan.id == plan_id)
        .options(selectinload(SubscriptionPlan.prices))
    )
    return result.scalar_one_or_none()


async def get_plan_price(
    db: AsyncSession,
    plan_id: int,
    period_days: int,
) -> Optional[int]:
    """Absolute price (kopeks) for (plan, period). None if combo not configured."""
    result = await db.execute(
        select(SubscriptionPlanPrice.price_kopeks).where(
            SubscriptionPlanPrice.plan_id == plan_id,
            SubscriptionPlanPrice.period_days == period_days,
        )
    )
    row = result.scalar_one_or_none()
    return int(row) if row is not None else None


def get_lowest_monthly_price(plan: SubscriptionPlan) -> Optional[int]:
    """Lowest per-month price across all configured periods, in kopeks.

    Used to render the "от X ₽/мес" line on each tariff card.
    """
    if not plan.prices:
        return None
    monthly_rates = [
        int(round(p.price_kopeks * 30 / p.period_days))
        for p in plan.prices
        if p.period_days > 0
    ]
    return min(monthly_rates) if monthly_rates else None


def calculate_upgrade_delta(
    current_subscription: Subscription,
    new_plan: SubscriptionPlan,
    new_plan_price_kopeks: int,
    current_plan_price_kopeks: Optional[int],
) -> int:
    """Prorated upgrade cost in kopeks for switching tiers mid-subscription.

    Inputs:
      current_subscription: with end_date and plan_period_days set
      new_plan: target SubscriptionPlan
      new_plan_price_kopeks: price for the new plan at the subscription's plan_period_days
      current_plan_price_kopeks: same, for the current plan (None if legacy)

    Formula:
      days_remaining = max(0, (end_date - now).days)
      period_days = subscription.plan_period_days or 30
      new_daily = new_price / period_days
      cur_daily = current_price / period_days   (0 if legacy / unset)
      return max(0, round((new_daily - cur_daily) * days_remaining))
    """
    now = datetime.utcnow()
    days_remaining = max(0, (current_subscription.end_date - now).days)
    period_days = current_subscription.plan_period_days or 30

    if period_days <= 0:
        return 0

    new_daily = new_plan_price_kopeks / period_days
    cur_daily = (current_plan_price_kopeks or 0) / period_days
    delta_per_day = new_daily - cur_daily
    return max(0, int(round(delta_per_day * days_remaining)))


async def get_current_plan_price_for_period(
    db: AsyncSession,
    subscription: Subscription,
) -> Optional[int]:
    """Helper for upgrade math: price of the user's current plan at their original period."""
    if subscription.plan_id is None or subscription.plan_period_days is None:
        return None
    return await get_plan_price(db, subscription.plan_id, subscription.plan_period_days)
