from datetime import date as date_type, datetime
from typing import Any, Dict, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import UserDailyMetric


async def upsert_user_daily_metric(
    db: AsyncSession,
    *,
    metric_date: date_type,
    metrics: Dict[str, Any],
) -> Tuple[UserDailyMetric, bool]:
    result = await db.execute(
        select(UserDailyMetric).where(UserDailyMetric.date == metric_date)
    )
    metric = result.scalar_one_or_none()
    created = metric is None

    values = {
        key: max(0, int(value or 0))
        for key, value in metrics.items()
        if key != "snapshot_at"
    }
    snapshot_at = metrics.get("snapshot_at") or datetime.utcnow()

    if metric is None:
        metric = UserDailyMetric(date=metric_date, snapshot_at=snapshot_at, **values)
        db.add(metric)
    else:
        metric.snapshot_at = snapshot_at
        for key, value in values.items():
            setattr(metric, key, value)
        metric.updated_at = datetime.utcnow()

    await db.flush()
    return metric, created
