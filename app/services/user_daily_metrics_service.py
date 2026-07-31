import logging
from datetime import datetime, time, timedelta, timezone
from typing import Any, Callable, Dict, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.system_setting import upsert_system_setting
from app.database.crud.user_daily_metric import upsert_user_daily_metric
from app.database.models import SystemSetting, User, UserDailyMetric, UserStatus


logger = logging.getLogger(__name__)

LAST_SNAPSHOT_SETTING_KEY = "user_daily_metrics_last_snapshot_date"
MOSCOW_TZ = ZoneInfo("Europe/Moscow")
UTC_TZ = timezone.utc


class UserDailyMetricsService:
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
                "Last collected daily user metrics snapshot date",
            )
            await db.commit()
            return {
                "skipped": True,
                "reason": "already_collected",
                "date": target_date_iso,
            }

        return await self._collect_for_date(db, target_date)

    async def collect_missing_recent_days(
        self,
        db: AsyncSession,
        days: int = 30,
    ) -> Dict[str, Any]:
        end_date = self._yesterday_moscow_date()
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
                    "Ошибка сбора snapshot метрик пользователей за %s: %s",
                    current_date_iso,
                    error,
                )

            current_date += timedelta(days=1)

        await upsert_system_setting(
            db,
            LAST_SNAPSHOT_SETTING_KEY,
            end_date.isoformat(),
            "Last checked daily user metrics snapshot date",
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

    async def _collect_for_date(self, db: AsyncSession, target_date) -> Dict[str, Any]:
        target_date_iso = target_date.isoformat()
        range_start, range_end = self._date_range_utc_naive(target_date)
        snapshot_at = self._snapshot_at()
        metrics = await self._collect_metrics(db, range_start, range_end, snapshot_at)

        _, created = await upsert_user_daily_metric(
            db,
            metric_date=target_date,
            metrics={**metrics, "snapshot_at": snapshot_at},
        )
        await upsert_system_setting(
            db,
            LAST_SNAPSHOT_SETTING_KEY,
            target_date_iso,
            "Last collected daily user metrics snapshot date",
        )
        await db.commit()

        result = {
            "skipped": False,
            "date": target_date_iso,
            "range_start": range_start.isoformat(),
            "range_end": range_end.isoformat(),
            "snapshot_at": snapshot_at.isoformat(),
            "created": created,
            **metrics,
        }
        logger.info("✅ Snapshot метрик пользователей сохранён: %s", result)
        return result

    async def _collect_metrics(
        self,
        db: AsyncSession,
        range_start: datetime,
        range_end: datetime,
        snapshot_at: datetime,
    ) -> Dict[str, int]:
        new_user_filter = and_(User.created_at >= range_start, User.created_at <= range_end)
        active_promo_filter = and_(
            User.promo_offer_discount_percent > 0,
            or_(
                User.promo_offer_discount_expires_at.is_(None),
                User.promo_offer_discount_expires_at > snapshot_at,
            ),
        )

        new_telegram_users_count = await self._count(
            db,
            new_user_filter,
            User.telegram_id.isnot(None),
        )
        telegram_users_count = await self._count(db, User.telegram_id.isnot(None))

        return {
            "new_users_count": await self._count(db, new_user_filter),
            "new_telegram_users_count": new_telegram_users_count,
            "new_bot_users_count": new_telegram_users_count,
            "new_web_users_count": await self._count(db, new_user_filter, User.auth_source == "web"),
            "new_app_users_count": await self._count(db, new_user_filter, User.auth_source == "app"),
            "new_email_users_count": await self._count(db, new_user_filter, User.email.isnot(None)),
            "new_referred_users_count": await self._count(db, new_user_filter, User.referred_by_id.isnot(None)),
            "total_users_count": await self._count(db),
            "active_users_count": await self._count(db, User.status == UserStatus.ACTIVE.value),
            "blocked_users_count": await self._count(db, User.status == UserStatus.BLOCKED.value),
            "deleted_users_count": await self._count(db, User.status == UserStatus.DELETED.value),
            "telegram_users_count": telegram_users_count,
            "bot_users_count": telegram_users_count,
            "web_users_count": await self._count(db, User.auth_source == "web"),
            "app_users_count": await self._count(db, User.auth_source == "app"),
            "email_users_count": await self._count(db, User.email.isnot(None)),
            "users_with_remnawave_uuid_count": await self._count(db, User.remnawave_uuid.isnot(None)),
            "users_connected_to_vpn_count": await self._count(db, User.has_connected_to_vpn.is_(True)),
            "users_without_vpn_connection_count": await self._count(db, User.has_connected_to_vpn.is_(False)),
            "users_with_first_topup_count": await self._count(db, User.has_made_first_topup.is_(True)),
            "users_with_paid_subscription_history_count": await self._count(db, User.has_had_paid_subscription.is_(True)),
            "users_with_positive_balance_count": await self._count(db, User.balance_kopeks > 0),
            "total_balance_kopeks": await self._sum(db, User.balance_kopeks),
            "referred_users_count": await self._count(db, User.referred_by_id.isnot(None)),
            "users_with_referral_code_count": await self._count(db, User.referral_code.isnot(None)),
            "users_with_custom_referral_commission_count": await self._count(db, User.referral_commission_percent.isnot(None)),
            "qualified_referrers_count": await self._count(db, User.qualified_referrals_count > 0),
            "total_qualified_referrals_count": await self._sum(db, User.qualified_referrals_count),
            "mobile_app_users_count": await self._count(db, User.has_used_mobile_app.is_(True)),
            "users_with_tg_user_id_count": await self._count(db, User.tg_user_id.isnot(None)),
            "users_with_acquisition_source_count": await self._count(db, User.acquisition_source.isnot(None)),
            "users_with_attribution_source_count": await self._count(db, User.attribution_source.isnot(None)),
            "users_with_attribution_campaign_count": await self._count(db, User.attribution_campaign_id.isnot(None)),
            "users_with_promo_group_count": await self._count(db, User.promo_group_id.isnot(None)),
            "users_with_auto_promo_group_count": await self._count(db, User.auto_promo_group_assigned.is_(True)),
            "users_with_active_promo_offer_count": await self._count(db, active_promo_filter),
            "legacy_pricing_users_count": await self._count(db, User.tariff_pricing_cohort_override == "legacy"),
            "new_pricing_users_count": await self._count(db, User.tariff_pricing_cohort_override == "new"),
        }

    async def _count(self, db: AsyncSession, *conditions: Any) -> int:
        query = select(func.count(User.id))
        if conditions:
            query = query.where(and_(*conditions))
        result = await db.execute(query)
        return int(result.scalar() or 0)

    async def _sum(self, db: AsyncSession, column: Any) -> int:
        result = await db.execute(select(func.coalesce(func.sum(column), 0)))
        return int(result.scalar() or 0)

    async def _get_last_snapshot_date(self, db: AsyncSession) -> Optional[str]:
        result = await db.execute(
            select(SystemSetting.value).where(SystemSetting.key == LAST_SNAPSHOT_SETTING_KEY)
        )
        value = result.scalar_one_or_none()
        return value.strip() if value else None

    async def _snapshot_exists(self, db: AsyncSession, target_date) -> bool:
        result = await db.execute(
            select(UserDailyMetric.id).where(UserDailyMetric.date == target_date)
        )
        return result.scalar_one_or_none() is not None

    def _yesterday_moscow_date(self):
        now = self._now_provider()
        if now.tzinfo is None:
            now = now.replace(tzinfo=MOSCOW_TZ)
        return now.astimezone(MOSCOW_TZ).date() - timedelta(days=1)

    def _date_range_utc_naive(self, target_date) -> tuple[datetime, datetime]:
        start_local = datetime.combine(target_date, time.min, tzinfo=MOSCOW_TZ)
        end_local = datetime.combine(target_date, time.max, tzinfo=MOSCOW_TZ)
        start_utc = start_local.astimezone(UTC_TZ).replace(tzinfo=None)
        end_utc = end_local.astimezone(UTC_TZ).replace(tzinfo=None)
        return start_utc, end_utc

    def _snapshot_at(self) -> datetime:
        now = self._now_provider()
        if now.tzinfo is None:
            return now
        return now.astimezone(UTC_TZ).replace(tzinfo=None)


user_daily_metrics_service = UserDailyMetricsService()
