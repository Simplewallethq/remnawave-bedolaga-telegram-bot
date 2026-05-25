"""GET /api/plans — exposes the tiered subscription catalog (App/Solo/Plus/Pro)
to the teleVpn backend so it can render the purchase list in the frontend."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Security
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.plan_pricing_service import list_active_plans

from ..dependencies import get_db_session, require_api_token
from ..schemas.plans import PlanItem, PlanPriceItem, PlansListResponse

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get(
    "",
    response_model=PlansListResponse,
    summary="List active subscription plans with price ladders",
)
async def list_plans(
    _=Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> PlansListResponse:
    """Returns all active plans (App/Solo/Plus/Pro) ordered by sort_order.

    Each plan includes its full price ladder (30/90/180/360/720 day prices)
    and the feature flags (custom_app_only, priority_support, device_limit,
    traffic_limit_gb) needed by the frontend to render the purchase cards
    and gate which client features to enable for each tier.
    """
    plans = await list_active_plans(db)
    items = [
        PlanItem(
            id=plan.id,
            code=plan.code,
            display_name=plan.display_name,
            device_limit=plan.device_limit,
            traffic_limit_gb=plan.traffic_limit_gb,
            traffic_reset_strategy=plan.traffic_reset_strategy,
            custom_app_only=plan.custom_app_only,
            priority_support=plan.priority_support,
            sort_order=plan.sort_order,
            is_active=plan.is_active,
            description_md=plan.description_md,
            prices=[
                PlanPriceItem(period_days=p.period_days, price_kopeks=p.price_kopeks)
                for p in plan.prices
            ],
        )
        for plan in plans
    ]
    return PlansListResponse(plans=items)
