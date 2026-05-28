"""Schemas for GET /api/plans — consumed by teleVpn backend to render the
purchase catalog in the miniapp/frontend. Plans correspond to rows in
`subscription_plans` (App / Solo / Plus / Pro)."""

from __future__ import annotations

from typing import List, Literal, Optional

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


# ---------- Backward-compatibility catalog for legacy à-la-carte users ----------

class LegacyPeriodOption(BaseModel):
    period_days: int
    price_kopeks: int


class LegacyTrafficAddon(BaseModel):
    gb: int = Field(..., description="0 means unlimited.")
    price_kopeks: int


class LegacyDeviceAddon(BaseModel):
    included: int = Field(..., description="Devices bundled with a period purchase.")
    max: int
    price_per_extra_kopeks: int


class LegacyDefaults(BaseModel):
    default_traffic_gb: int
    default_device_limit: int


class LegacyCatalog(BaseModel):
    periods: List[LegacyPeriodOption] = Field(default_factory=list)
    traffic_addons: List[LegacyTrafficAddon] = Field(default_factory=list)
    device_addon: LegacyDeviceAddon
    defaults: LegacyDefaults


class TrialInfo(BaseModel):
    days_left: int
    traffic_used_gb: float
    traffic_limit_gb: int


class ForDeviceResponse(BaseModel):
    """User-aware tariff catalog. `plans` is always populated with the new
    tiered catalog. `legacy_catalog` is set only for users still on the old
    à-la-carte model. `trial_info` is set when the linked subscription is
    currently on trial."""
    user_type: Literal["new", "legacy", "trial"]
    plans: List[PlanItem] = Field(default_factory=list)
    legacy_catalog: Optional[LegacyCatalog] = None
    trial_info: Optional[TrialInfo] = None
