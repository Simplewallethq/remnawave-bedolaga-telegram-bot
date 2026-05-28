from __future__ import annotations

from fastapi import APIRouter, Depends, Security
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.ad_attribution import get_ad_campaign_stats

from ..dependencies import get_db_session, require_api_token
from ..schemas.ad_attribution import AdCampaignStatsResponse


router = APIRouter()


@router.get("/{campaign_id}/stats", response_model=AdCampaignStatsResponse)
async def get_campaign_stats(
    campaign_id: str,
    db: AsyncSession = Depends(get_db_session),
    _token=Security(require_api_token),
):
    return await get_ad_campaign_stats(db, campaign_id)
