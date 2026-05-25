"""Schemas for GET /api/plans — consumed by teleVpn backend to render the
purchase catalog in the miniapp/frontend. Plans correspond to rows in
`subscription_plans` (App / Solo / Plus / Pro)."""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class PlanPriceItem(BaseModel):
    """One row of subscription_plan_prices."""
    period_days: int = Field(..., description="Billing period in days (30/90/180/360/720).")
    price_kopeks: int = Field(..., description="Absolute price in kopeks for this period.")


class PlanItem(BaseModel):
    """One row of subscription_plans with its price ladder."""
    id: int
    code: str = Field(..., description="Machine code (eng): app/solo/plus/pro.")
    display_name: str = Field(..., description="Human-readable name (eng).")
    device_limit: int
    traffic_limit_gb: int = Field(..., description="0 means unlimited.")
    traffic_reset_strategy: str = Field(..., description="MONTH | NO_RESET | ...")
    custom_app_only: bool = Field(..., description="True for App-only tariffs (no full VPN).")
    priority_support: bool
    sort_order: int
    is_active: bool
    description_md: Optional[str] = None
    prices: List[PlanPriceItem] = Field(default_factory=list)


class PlansListResponse(BaseModel):
    plans: List[PlanItem]
