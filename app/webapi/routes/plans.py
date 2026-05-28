"""GET /api/plans — exposes the tiered subscription catalog (App/Solo/Plus/Pro)
to the teleVpn backend so it can render the purchase list in the frontend.

GET /api/plans/for-device/{device_id} additionally classifies the device's owner
as new/legacy/trial and, for legacy users, attaches the à-la-carte catalog so
old clients can still see the pricing they originally signed up under."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Security, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import PERIOD_PRICES, settings
from app.database.crud.device_link import get_subscription_by_device_id
from app.database.models import User
from app.handlers.subscription.purchase import user_uses_tariffs
from app.services.plan_pricing_service import list_active_plans

from ..dependencies import get_db_session, require_api_token
from ..schemas.plans import (
    ForDeviceResponse,
    LegacyCatalog,
    LegacyDefaults,
    LegacyDeviceAddon,
    LegacyPeriodOption,
    LegacyTrafficAddon,
    PlanItem,
    PlanPriceItem,
    PlansListResponse,
    TrialInfo,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _build_plan_items(plans) -> list[PlanItem]:
    return [
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


def _build_legacy_catalog() -> LegacyCatalog:
    """Snapshot the legacy à-la-carte catalog from current settings.

    Periods come from PERIOD_PRICES (0-price entries skipped — disabled).
    Traffic addons come from settings.get_traffic_packages() (disabled
    packages skipped). Device pricing is the per-extra flat price."""
    periods = [
        LegacyPeriodOption(period_days=days, price_kopeks=price_kopeks)
        for days, price_kopeks in sorted(PERIOD_PRICES.items())
        if price_kopeks and price_kopeks > 0
    ]
    traffic_addons = [
        LegacyTrafficAddon(gb=pkg["gb"], price_kopeks=pkg["price"])
        for pkg in settings.get_traffic_packages()
        if pkg.get("enabled", True)
    ]
    return LegacyCatalog(
        periods=periods,
        traffic_addons=traffic_addons,
        device_addon=LegacyDeviceAddon(
            included=settings.DEFAULT_DEVICE_LIMIT,
            max=settings.MAX_DEVICES_LIMIT,
            price_per_extra_kopeks=settings.PRICE_PER_DEVICE,
        ),
        defaults=LegacyDefaults(
            default_traffic_gb=settings.DEFAULT_TRAFFIC_LIMIT_GB,
            default_device_limit=settings.DEFAULT_DEVICE_LIMIT,
        ),
    )


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
    return PlansListResponse(plans=_build_plan_items(plans))


@router.get(
    "/for-device/{device_id}",
    response_model=ForDeviceResponse,
    summary="Tariff options for a device's owner (legacy/new/trial-aware)",
    responses={404: {"description": "Device not linked to any subscription"}},
)
async def get_tariffs_for_device(
    device_id: str,
    _=Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> ForDeviceResponse:
    """Resolve the subscription behind device_id, classify its owner, and
    return: the always-on new-tariffs catalog, plus a legacy à-la-carte
    catalog for users still on the pre-tariffs pricing model.

    Classification rules mirror handlers.subscription.purchase.user_uses_tariffs:
    new users (tariffs flow + non-trial) → "new",
    trial subscriptions → "trial",
    everyone else → "legacy" (registered before TARIFFS_LEGACY_CUTOFF with no plan).
    """
    subscription = await get_subscription_by_device_id(db, device_id)
    if subscription is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Device not linked to any subscription",
        )

    # Explicitly load the User row (Subscription.user is lazy and would emit
    # an implicit-IO error in async mode).
    user_result = await db.execute(select(User).where(User.id == subscription.user_id))
    db_user = user_result.scalar_one()
    # user_uses_tariffs reads db_user.subscription.plan_id; attach the already
    # loaded subscription so it can be used without another DB round-trip.
    db_user.subscription = subscription

    if user_uses_tariffs(db_user):
        user_type = "trial" if subscription.is_trial else "new"
    else:
        user_type = "legacy"

    plans = await list_active_plans(db)
    response = ForDeviceResponse(
        user_type=user_type,
        plans=_build_plan_items(plans),
    )

    if user_type == "legacy":
        response.legacy_catalog = _build_legacy_catalog()
    if user_type == "trial":
        response.trial_info = TrialInfo(
            days_left=subscription.days_left,
            traffic_used_gb=float(subscription.traffic_used_gb or 0.0),
            traffic_limit_gb=int(subscription.traffic_limit_gb or 0),
        )

    return response
