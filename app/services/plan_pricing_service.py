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

from app.config import settings
from app.database.models import Subscription, SubscriptionPlan, SubscriptionPlanPrice

logger = logging.getLogger(__name__)


SUPPORTED_PERIOD_DAYS: List[int] = [30, 90, 360, 720]


def resolve_pricing_cohort(db_user) -> str:
    """Pricing cohort for a user: 'scheme:<id>' when the bot they are buying in has
    a price scheme (see price_scheme_service), else 'new' if they registered at/after
    the new-pricing cutoff, else 'legacy'. None-safe — missing user/created_at/cutoff
    falls back to 'legacy' (the grandfathered, current-prices set)."""
    from app.services.price_scheme_service import scheme_cohort, scheme_for_user

    scheme = scheme_for_user(db_user)
    if scheme is not None:
        return scheme_cohort(scheme.id)

    override = str(getattr(db_user, "tariff_pricing_cohort_override", None) or "").lower()
    if override in ("new", "legacy"):
        return override

    cutoff = settings.get_tariffs_new_pricing_cutoff()
    created_at = getattr(db_user, "created_at", None)
    if cutoff is not None and created_at is not None and created_at >= cutoff:
        return "new"
    return "legacy"


def select_prices_for_audience(plan: SubscriptionPlan, cohort: str) -> dict:
    """{period_days: price_kopeks} from the plan's own price rows — rows tagged 'all'
    plus the cohort's own rows. `plan.prices` is already eagerly loaded."""
    return {
        p.period_days: p.price_kopeks
        for p in plan.prices
        if getattr(p, "audience", "all") in ("all", cohort)
    }


def select_prices_for_cohort(plan: SubscriptionPlan, cohort: str) -> dict:
    """{period_days: price_kopeks} for the cohort. A price-scheme cohort takes the
    scheme's prices on top of the 'new' ones; filtering is in memory."""
    from app.services.price_scheme_service import (
        BASE_COHORT,
        get_scheme,
        scheme_id_from_cohort,
        scheme_prices_for_plan,
    )

    scheme_id = scheme_id_from_cohort(cohort)
    if scheme_id is None:
        return select_prices_for_audience(plan, cohort)
    base = select_prices_for_audience(plan, BASE_COHORT)
    scheme = get_scheme(scheme_id)
    if scheme is None:
        return base
    return scheme_prices_for_plan(scheme, plan.code, base)


def plan_device_limit(plan: SubscriptionPlan, cohort: Optional[str] = None) -> int:
    """Devices included in the plan — the scheme's own number when it sets one."""
    from app.services.price_scheme_service import get_scheme, scheme_device_limit, scheme_id_from_cohort

    limit = scheme_device_limit(get_scheme(scheme_id_from_cohort(cohort)), plan.code)
    return int(limit) if limit is not None else int(plan.device_limit)


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
    cohort: str = "new",
) -> Optional[int]:
    """Absolute price (kopeks) for (plan, period) in the given cohort. None if not
    configured. A period is either tagged 'all' or split per-cohort, never both, so
    filtering audience IN ('all', cohort) yields at most one row. A price-scheme
    cohort reads the scheme's prices, falling back to the 'new' ones."""
    from app.services.price_scheme_service import (
        BASE_COHORT,
        get_scheme,
        plan_code_for_id,
        scheme_id_from_cohort,
        scheme_prices_for_plan,
    )

    scheme_id = scheme_id_from_cohort(cohort)
    if scheme_id is not None:
        base = await get_plan_price(db, plan_id, period_days, cohort=BASE_COHORT)
        scheme = get_scheme(scheme_id)
        if scheme is None:
            return base
        fallback = {period_days: base} if base is not None else {}
        return scheme_prices_for_plan(scheme, plan_code_for_id(plan_id), fallback).get(period_days)

    result = await db.execute(
        select(SubscriptionPlanPrice.price_kopeks).where(
            SubscriptionPlanPrice.plan_id == plan_id,
            SubscriptionPlanPrice.period_days == period_days,
            SubscriptionPlanPrice.audience.in_(("all", cohort)),
        )
    )
    row = result.scalar_one_or_none()
    return int(row) if row is not None else None


def get_lowest_monthly_price(plan: SubscriptionPlan, cohort: str = "new") -> Optional[int]:
    """Headline monthly price (kopeks) for the cohort — the 30-day tariff price as-is.

    (Longer periods carry a discount, but we don't advertise the cheapest-per-month rate
    to avoid setting a wrong price expectation.)
    """
    prices = select_prices_for_cohort(plan, cohort)
    if not prices:
        return None
    if 30 in prices:
        return int(prices[30])
    # Fallback if no 30-day price configured — use the lowest effective monthly rate.
    monthly_rates = [
        int(round(price * 30 / period_days))
        for period_days, price in prices.items()
        if period_days > 0
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
    db_user=None,
) -> Optional[int]:
    """Helper for upgrade math: price of the user's current plan at their original period.

    Uses the user's cohort so a legacy user on the 180-day period still resolves their
    (legacy) price. Defaults to 'legacy' when no user is supplied, since an existing
    subscription was bought under the grandfathered set.
    """
    if subscription.plan_id is None or subscription.plan_period_days is None:
        return None
    cohort = resolve_pricing_cohort(db_user) if db_user is not None else "legacy"
    return await get_plan_price(
        db, subscription.plan_id, subscription.plan_period_days, cohort=cohort
    )
