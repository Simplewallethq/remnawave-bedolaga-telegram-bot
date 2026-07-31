from datetime import date as date_type, datetime
from typing import Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import TrialExpiryDailyMetric


async def upsert_trial_expiry_daily_metric(
    db: AsyncSession,
    *,
    metric_date: date_type,
    snapshot_at: datetime,
    trial_ended_count: int,
    trial_paid_7d_count: int,
    connected_trial_ended_count: int,
    connected_trial_paid_7d_count: int,
) -> Tuple[TrialExpiryDailyMetric, bool]:
    result = await db.execute(
        select(TrialExpiryDailyMetric).where(TrialExpiryDailyMetric.date == metric_date)
    )
    metric = result.scalar_one_or_none()
    created = metric is None

    values = {
        "trial_ended_count": trial_ended_count,
        "trial_paid_7d_count": trial_paid_7d_count,
        "connected_trial_ended_count": connected_trial_ended_count,
        "connected_trial_paid_7d_count": connected_trial_paid_7d_count,
    }
    values = {key: max(0, int(value or 0)) for key, value in values.items()}

    if metric is None:
        metric = TrialExpiryDailyMetric(
            date=metric_date,
            snapshot_at=snapshot_at,
            **values,
        )
        db.add(metric)
    else:
        metric.snapshot_at = snapshot_at
        for key, value in values.items():
            setattr(metric, key, value)
        metric.updated_at = datetime.utcnow()

    await db.flush()
    return metric, created
