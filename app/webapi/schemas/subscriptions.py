from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


class SubscriptionResponse(BaseModel):
    id: int
    user_id: int
    status: str
    actual_status: str
    is_trial: bool
    used_trial_failed: bool = False
    start_date: datetime
    end_date: datetime
    traffic_limit_gb: int
    traffic_used_gb: float
    device_limit: int
    # Тариф из каталога subscription_plans (App/Solo/Plus/Pro). None у легаси-
    # подписок и триалов, оформленных до тарифной сетки — у них plan_id в БД
    # пустой. Потребители (teleVpn) используют plan_id как id тарифа, совпадающий
    # с id в GET /api/plans; plan_code отдаётся рядом, чтобы не ходить в каталог
    # ради машинного кода тарифа.
    plan_id: Optional[int] = None
    plan_code: Optional[str] = None
    plan_period_days: Optional[int] = None
    autopay_enabled: bool
    autopay_days_before: Optional[int] = None
    subscription_url: Optional[str] = None
    subscription_crypto_link: Optional[str] = None
    connected_squads: List[str] = Field(default_factory=list)
    connected_devices: int = 0
    remnawave_user_uuid: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class SubscriptionCreateRequest(BaseModel):
    user_id: int
    is_trial: bool = False
    duration_days: Optional[int] = None
    traffic_limit_gb: Optional[int] = None
    device_limit: Optional[int] = None
    squad_uuid: Optional[str] = None
    connected_squads: Optional[List[str]] = None
    replace_existing: bool = False


class SubscriptionExtendRequest(BaseModel):
    days: int = Field(..., gt=0)


class SubscriptionTrafficRequest(BaseModel):
    gb: int = Field(..., gt=0)


class SubscriptionDevicesRequest(BaseModel):
    devices: int = Field(..., gt=0)


class SubscriptionSquadRequest(BaseModel):
    squad_uuid: str
