from __future__ import annotations

from pydantic import BaseModel


class AdCampaignStatsResponse(BaseModel):
    campaign_id: str
    arrived: int
    registered: int
    paid: int
    reg_conversion_pct: float
    pay_conversion_pct: float
