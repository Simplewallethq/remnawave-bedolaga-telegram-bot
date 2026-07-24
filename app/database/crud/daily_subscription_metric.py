from datetime import date as date_type, datetime
from typing import Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import DailySubscriptionMetric


async def upsert_daily_subscription_metric(
    db: AsyncSession,
    *,
    metric_date: date_type,
    paid_users_count: int,
    lost_paid_users_count: int,
) -> Tuple[DailySubscriptionMetric, bool]:
    result = await db.execute(
        select(DailySubscriptionMetric).where(DailySubscriptionMetric.date == metric_date)
    )
    metric = result.scalar_one_or_none()
    created = metric is None

    if metric is None:
        metric = DailySubscriptionMetric(
            date=metric_date,
            paid_users_count=max(0, int(paid_users_count or 0)),
            lost_paid_users_count=max(0, int(lost_paid_users_count or 0)),
        )
        db.add(metric)
    else:
        metric.paid_users_count = max(0, int(paid_users_count or 0))
        metric.lost_paid_users_count = max(0, int(lost_paid_users_count or 0))
        metric.updated_at = datetime.utcnow()

    await db.flush()
    return metric, created
