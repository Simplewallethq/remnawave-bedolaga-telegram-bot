import logging
from datetime import datetime, time, timedelta, timezone
from typing import Any, Callable, Dict, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.daily_subscription_metric import upsert_daily_subscription_metric
from app.database.crud.system_setting import upsert_system_setting
from app.database.models import DailySubscriptionMetric, Subscription, SystemSetting


logger = logging.getLogger(__name__)

LAST_SNAPSHOT_SETTING_KEY = "daily_subscription_metrics_last_snapshot_date"
MOSCOW_TZ = ZoneInfo("Europe/Moscow")
UTC_TZ = timezone.utc


class DailySubscriptionMetricsService:
    def __init__(self, now_provider: Optional[Callable[[], datetime]] = None) -> None:
        self._now_provider = now_provider or (lambda: datetime.now(MOSCOW_TZ))

    async def collect_for_yesterday(self, db: AsyncSession) -> Dict[str, Any]:
        target_date = self._yesterday_moscow_date()
        target_date_iso = target_date.isoformat()

        last_snapshot_date = await self._get_last_snapshot_date(db)
        if last_snapshot_date == target_date_iso:
            return {
                "skipped": True,
                "reason": "already_collected",
                "date": target_date_iso,
            }

        if await self._snapshot_exists(db, target_date):
            await upsert_system_setting(
                db,
                LAST_SNAPSHOT_SETTING_KEY,
                target_date_iso,
                "Last collected daily subscription metrics snapshot date",
            )
            await db.commit()
            return {
                "skipped": True,
                "reason": "already_collected",
                "date": target_date_iso,
            }

        as_of = self._as_of_for_date(target_date)
        lost_cutoff = as_of - timedelta(hours=24)

        paid_users_count = await self._count_paid_users(db, as_of)
        lost_paid_users_count = await self._count_lost_paid_users(db, lost_cutoff)

        _, created = await upsert_daily_subscription_metric(
            db,
            metric_date=target_date,
            paid_users_count=paid_users_count,
            lost_paid_users_count=lost_paid_users_count,
        )
        await upsert_system_setting(
            db,
            LAST_SNAPSHOT_SETTING_KEY,
            target_date_iso,
            "Last collected daily subscription metrics snapshot date",
        )
        await db.commit()

        result = {
            "skipped": False,
            "date": target_date_iso,
            "as_of": as_of.isoformat(),
            "paid_users_count": paid_users_count,
            "lost_paid_users_count": lost_paid_users_count,
            "created": created,
        }
        logger.info("✅ Snapshot метрик подписок сохранён: %s", result)
        return result

    async def _get_last_snapshot_date(self, db: AsyncSession) -> Optional[str]:
        result = await db.execute(
            select(SystemSetting.value).where(SystemSetting.key == LAST_SNAPSHOT_SETTING_KEY)
        )
        value = result.scalar_one_or_none()
        return value.strip() if value else None

    async def _snapshot_exists(self, db: AsyncSession, target_date) -> bool:
        result = await db.execute(
            select(DailySubscriptionMetric.id).where(DailySubscriptionMetric.date == target_date)
        )
        return result.scalar_one_or_none() is not None

    async def _count_paid_users(self, db: AsyncSession, as_of: datetime) -> int:
        result = await db.execute(
            select(func.count(func.distinct(Subscription.user_id))).where(
                and_(
                    Subscription.is_trial.is_(False),
                    func.coalesce(Subscription.is_partner, False).is_(False),
                    Subscription.start_date <= as_of,
                    Subscription.end_date > as_of,
                )
            )
        )
        return int(result.scalar() or 0)

    async def _count_lost_paid_users(self, db: AsyncSession, lost_cutoff: datetime) -> int:
        result = await db.execute(
            select(func.count(func.distinct(Subscription.user_id))).where(
                and_(
                    Subscription.is_trial.is_(False),
                    func.coalesce(Subscription.is_partner, False).is_(False),
                    Subscription.end_date < lost_cutoff,
                )
            )
        )
        return int(result.scalar() or 0)

    def _yesterday_moscow_date(self):
        now = self._now_provider()
        if now.tzinfo is None:
            now = now.replace(tzinfo=MOSCOW_TZ)
        now_moscow = now.astimezone(MOSCOW_TZ)
        return now_moscow.date() - timedelta(days=1)

    def _as_of_for_date(self, target_date) -> datetime:
        end_local = datetime.combine(target_date, time.max, tzinfo=MOSCOW_TZ)
        return end_local.astimezone(UTC_TZ).replace(tzinfo=None)


daily_subscription_metrics_service = DailySubscriptionMetricsService()
