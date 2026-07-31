import logging
from datetime import date as date_type, datetime, time, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, Optional, Set
from zoneinfo import ZoneInfo

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.system_setting import upsert_system_setting
from app.database.crud.trial_expiry_daily_metric import upsert_trial_expiry_daily_metric
from app.database.models import SubscriptionEvent, SystemSetting, TrialExpiryDailyMetric, User


logger = logging.getLogger(__name__)

LAST_SNAPSHOT_SETTING_KEY = "trial_expiry_daily_metrics_last_snapshot_date"
MOSCOW_TZ = ZoneInfo("Europe/Moscow")
UTC_TZ = timezone.utc
LAG_DAYS = 7
ACTIVATION_LOOKBACK_DAYS = 90


class TrialExpiryDailyMetricsService:
    def __init__(self, now_provider: Optional[Callable[[], datetime]] = None) -> None:
        self._now_provider = now_provider or (lambda: datetime.now(MOSCOW_TZ))

    async def collect_ready_cohort(self, db: AsyncSession) -> Dict[str, Any]:
        target_date = self._target_date()
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
                "Last collected trial expiry daily metrics snapshot date",
            )
            await db.commit()
            return {
                "skipped": True,
                "reason": "already_collected",
                "date": target_date_iso,
            }

        return await self._collect_for_date(db, target_date)

    async def collect_missing_ready_cohorts(
        self,
        db: AsyncSession,
        days: int = 30,
    ) -> Dict[str, Any]:
        end_date = self._target_date()
        start_date = end_date - timedelta(days=max(1, days) - 1)

        checked = 0
        created = 0
        skipped = 0
        failed = 0
        created_dates: list[str] = []
        skipped_dates: list[str] = []
        failed_dates: list[str] = []

        current_date = start_date
        while current_date <= end_date:
            checked += 1
            current_date_iso = current_date.isoformat()
            try:
                if await self._snapshot_exists(db, current_date):
                    skipped += 1
                    skipped_dates.append(current_date_iso)
                else:
                    await self._collect_for_date(db, current_date)
                    created += 1
                    created_dates.append(current_date_iso)
            except Exception as error:
                failed += 1
                failed_dates.append(current_date_iso)
                await db.rollback()
                logger.error(
                    "Ошибка сбора snapshot конверсии истёкших триалов за %s: %s",
                    current_date_iso,
                    error,
                )

            current_date += timedelta(days=1)

        await upsert_system_setting(
            db,
            LAST_SNAPSHOT_SETTING_KEY,
            end_date.isoformat(),
            "Last checked trial expiry daily metrics snapshot date",
        )
        await db.commit()

        return {
            "skipped": False,
            "batch": True,
            "range_start": start_date.isoformat(),
            "range_end": end_date.isoformat(),
            "checked": checked,
            "created": created,
            "skipped_existing": skipped,
            "failed": failed,
            "created_dates": created_dates,
            "skipped_dates": skipped_dates,
            "failed_dates": failed_dates,
        }

    async def _collect_for_date(self, db: AsyncSession, target_date: date_type) -> Dict[str, Any]:
        target_date_iso = target_date.isoformat()
        target_start, target_end = self._date_range_utc_naive(target_date)
        paid_until, _ = self._date_range_utc_naive(target_date + timedelta(days=LAG_DAYS))
        snapshot_at = self._snapshot_at()

        activation_events = await self._get_activation_events(db, target_end)
        ended_user_ids, connected_ended_user_ids = await self._build_trial_ended_user_sets(
            activation_events,
            target_start,
            target_end,
        )
        paid_user_ids = await self._get_paid_user_ids(db, ended_user_ids, target_start, paid_until)
        connected_paid_user_ids = connected_ended_user_ids.intersection(paid_user_ids)

        _, created = await upsert_trial_expiry_daily_metric(
            db,
            metric_date=target_date,
            snapshot_at=snapshot_at,
            trial_ended_count=len(ended_user_ids),
            trial_paid_7d_count=len(paid_user_ids),
            connected_trial_ended_count=len(connected_ended_user_ids),
            connected_trial_paid_7d_count=len(connected_paid_user_ids),
        )
        await upsert_system_setting(
            db,
            LAST_SNAPSHOT_SETTING_KEY,
            target_date_iso,
            "Last collected trial expiry daily metrics snapshot date",
        )
        await db.commit()

        result = {
            "skipped": False,
            "date": target_date_iso,
            "range_start": target_start.isoformat(),
            "range_end": target_end.isoformat(),
            "paid_until": paid_until.isoformat(),
            "snapshot_at": snapshot_at.isoformat(),
            "created": created,
            "trial_ended_count": len(ended_user_ids),
            "trial_paid_7d_count": len(paid_user_ids),
            "connected_trial_ended_count": len(connected_ended_user_ids),
            "connected_trial_paid_7d_count": len(connected_paid_user_ids),
        }
        logger.info("✅ Snapshot конверсии истёкших триалов сохранён: %s", result)
        return result

    async def _get_activation_events(
        self,
        db: AsyncSession,
        target_end: datetime,
    ) -> list[SubscriptionEvent]:
        lookback_start = target_end - timedelta(days=ACTIVATION_LOOKBACK_DAYS)
        result = await db.execute(
            select(SubscriptionEvent, User.has_connected_to_vpn)
            .join(User, User.id == SubscriptionEvent.user_id)
            .where(
                and_(
                    SubscriptionEvent.event_type == "activation",
                    SubscriptionEvent.occurred_at >= lookback_start,
                    SubscriptionEvent.occurred_at < target_end,
                )
            )
            .order_by(SubscriptionEvent.occurred_at.asc())
        )

        events: list[SubscriptionEvent] = []
        for event, has_connected_to_vpn in result.all():
            setattr(event, "_snapshot_has_connected_to_vpn", bool(has_connected_to_vpn))
            events.append(event)
        return events

    async def _build_trial_ended_user_sets(
        self,
        activation_events: Iterable[SubscriptionEvent],
        target_start: datetime,
        target_end: datetime,
    ) -> tuple[Set[int], Set[int]]:
        ended_user_ids: Set[int] = set()
        connected_ended_user_ids: Set[int] = set()

        for event in activation_events:
            trial_ended_at = self._extract_trial_ended_at(event)
            if not trial_ended_at:
                continue

            if target_start <= trial_ended_at < target_end:
                ended_user_ids.add(event.user_id)
                if getattr(event, "_snapshot_has_connected_to_vpn", False):
                    connected_ended_user_ids.add(event.user_id)

        return ended_user_ids, connected_ended_user_ids

    async def _get_paid_user_ids(
        self,
        db: AsyncSession,
        user_ids: Set[int],
        paid_from: datetime,
        paid_until: datetime,
    ) -> Set[int]:
        if not user_ids:
            return set()

        result = await db.execute(
            select(SubscriptionEvent.user_id)
            .where(
                and_(
                    SubscriptionEvent.user_id.in_(user_ids),
                    SubscriptionEvent.event_type == "purchase",
                    SubscriptionEvent.amount_kopeks > 0,
                    SubscriptionEvent.occurred_at >= paid_from,
                    SubscriptionEvent.occurred_at < paid_until,
                )
            )
            .distinct()
        )
        return {int(user_id) for user_id in result.scalars().all()}

    def _extract_trial_ended_at(self, event: SubscriptionEvent) -> Optional[datetime]:
        extra = event.extra if isinstance(event.extra, dict) else {}
        explicit_end = self._parse_datetime(
            extra.get("trial_ended_at")
            or extra.get("trial_end_at")
            or extra.get("trial_end_date")
        )
        if explicit_end:
            return explicit_end

        trial_started_at = self._parse_datetime(extra.get("trial_started_at")) or event.occurred_at
        trial_started_at = self._normalize_utc_naive(trial_started_at)

        try:
            duration_days = int(extra.get("trial_duration_days") or settings.TRIAL_DURATION_DAYS or 0)
        except (TypeError, ValueError):
            duration_days = int(settings.TRIAL_DURATION_DAYS or 0)

        if not trial_started_at or duration_days <= 0:
            return None
        return trial_started_at + timedelta(days=duration_days)

    def _target_date(self) -> date_type:
        now = self._now_provider()
        if now.tzinfo is None:
            now = now.replace(tzinfo=MOSCOW_TZ)
        return now.astimezone(MOSCOW_TZ).date() - timedelta(days=LAG_DAYS)

    def _date_range_utc_naive(self, value: date_type) -> tuple[datetime, datetime]:
        start_local = datetime.combine(value, time.min, tzinfo=MOSCOW_TZ)
        end_local = start_local + timedelta(days=1)
        return (
            start_local.astimezone(UTC_TZ).replace(tzinfo=None),
            end_local.astimezone(UTC_TZ).replace(tzinfo=None),
        )

    def _snapshot_at(self) -> datetime:
        now = self._now_provider()
        return self._normalize_utc_naive(now) or datetime.utcnow()

    async def _get_last_snapshot_date(self, db: AsyncSession) -> Optional[str]:
        result = await db.execute(
            select(SystemSetting.value).where(SystemSetting.key == LAST_SNAPSHOT_SETTING_KEY)
        )
        value = result.scalar_one_or_none()
        return value.strip() if value else None

    async def _snapshot_exists(self, db: AsyncSession, target_date) -> bool:
        result = await db.execute(
            select(TrialExpiryDailyMetric.id).where(TrialExpiryDailyMetric.date == target_date)
        )
        return result.scalar_one_or_none() is not None

    @classmethod
    def _parse_datetime(cls, value: Any) -> Optional[datetime]:
        if not value:
            return None
        if isinstance(value, datetime):
            return cls._normalize_utc_naive(value)
        if isinstance(value, str):
            try:
                return cls._normalize_utc_naive(datetime.fromisoformat(value.replace("Z", "+00:00")))
            except ValueError:
                return None
        return None

    @staticmethod
    def _normalize_utc_naive(value: Optional[datetime]) -> Optional[datetime]:
        if value is None:
            return None
        if value.tzinfo is None:
            return value
        return value.astimezone(UTC_TZ).replace(tzinfo=None)


trial_expiry_daily_metrics_service = TrialExpiryDailyMetricsService()
