from datetime import date as date_type, datetime
from typing import Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import UserDailyTrafficUsage


async def upsert_user_daily_traffic_usage(
    db: AsyncSession,
    *,
    user_id: int,
    usage_date: date_type,
    traffic_bytes: int,
) -> Tuple[UserDailyTrafficUsage, bool]:
    result = await db.execute(
        select(UserDailyTrafficUsage).where(
            UserDailyTrafficUsage.user_id == user_id,
            UserDailyTrafficUsage.date == usage_date,
        )
    )
    usage = result.scalar_one_or_none()
    created = usage is None

    if usage is None:
        usage = UserDailyTrafficUsage(
            user_id=user_id,
            date=usage_date,
            traffic_bytes=max(0, int(traffic_bytes or 0)),
        )
        db.add(usage)
    else:
        usage.traffic_bytes = max(0, int(traffic_bytes or 0))
        usage.updated_at = datetime.utcnow()

    await db.flush()
    return usage, created


async def get_latest_usage_date(db: AsyncSession) -> Optional[date_type]:
    result = await db.execute(select(func.max(UserDailyTrafficUsage.date)))
    return result.scalar_one_or_none()
