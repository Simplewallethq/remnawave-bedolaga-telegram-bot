import logging
from collections.abc import Iterable
from datetime import datetime, time, timedelta, timezone
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.system_setting import upsert_system_setting
from app.database.crud.user_daily_traffic_usage import upsert_user_daily_traffic_usage
from app.database.models import Subscription, SubscriptionStatus, SystemSetting, User, UserStatus
from app.services.remnawave_service import RemnaWaveService


logger = logging.getLogger(__name__)


LAST_SNAPSHOT_SETTING_KEY = "user_daily_traffic_last_snapshot_date"
MOSCOW_TZ = ZoneInfo("Europe/Moscow")
UTC_TZ = timezone.utc


class UserDailyTrafficUsageService:
    def __init__(self, remnawave_service: Optional[RemnaWaveService] = None) -> None:
        self.remnawave_service = remnawave_service or RemnaWaveService()

    async def collect_yesterday_once_after_deploy(self, db: AsyncSession) -> Dict[str, Any]:
        target_date = self._yesterday_moscow_date()
        last_snapshot_date = await self._get_last_snapshot_date(db)

        if last_snapshot_date == target_date.isoformat():
            return {
                "skipped": True,
                "reason": "already_collected",
                "date": target_date.isoformat(),
            }

        if not self.remnawave_service.is_configured:
            logger.warning("RemnaWave API не настроен. Пропускаем сбор дневного трафика")
            return {
                "skipped": True,
                "reason": "remnawave_not_configured",
                "date": target_date.isoformat(),
            }

        users = await self._get_candidate_users(db)
        if users:
            await db.commit()

        start_iso, end_iso = self._build_remnawave_range(target_date)
        processed = 0
        created = 0
        updated = 0
        failed = 0
        total_bytes = 0

        if users:
            async with self.remnawave_service.get_api_client() as api:
                for user in users:
                    try:
                        traffic_data = await api.get_user_stats_usage(
                            user.remnawave_uuid,
                            start_iso,
                            end_iso,
                        )
                        traffic_bytes = self._extract_total_bytes(traffic_data)
                        _, was_created = await upsert_user_daily_traffic_usage(
                            db,
                            user_id=user.id,
                            usage_date=target_date,
                            traffic_bytes=traffic_bytes,
                        )
                        await db.commit()

                        processed += 1
                        total_bytes += traffic_bytes
                        if was_created:
                            created += 1
                        else:
                            updated += 1
                    except Exception as error:
                        failed += 1
                        await db.rollback()
                        logger.warning(
                            "Не удалось собрать дневной трафик пользователя %s за %s: %s",
                            getattr(user, "telegram_id", None),
                            target_date.isoformat(),
                            error,
                        )

        await upsert_system_setting(
            db,
            LAST_SNAPSHOT_SETTING_KEY,
            target_date.isoformat(),
            "Last successfully attempted user daily traffic snapshot date",
        )
        await db.commit()

        result = {
            "skipped": False,
            "date": target_date.isoformat(),
            "range_start": start_iso,
            "range_end": end_iso,
            "candidates": len(users),
            "processed": processed,
            "created": created,
            "updated": updated,
            "failed": failed,
            "total_bytes": total_bytes,
        }
        logger.info("✅ Сбор дневного трафика завершён: %s", result)
        return result

    async def _get_candidate_users(self, db: AsyncSession) -> list[User]:
        result = await db.execute(
            select(User)
            .join(Subscription, Subscription.user_id == User.id)
            .where(
                and_(
                    User.telegram_id.isnot(None),
                    User.remnawave_uuid.isnot(None),
                    User.status == UserStatus.ACTIVE.value,
                    Subscription.status == SubscriptionStatus.ACTIVE.value,
                    Subscription.is_trial == False,  # noqa: E712
                )
            )
            .order_by(User.id)
        )
        return list(result.scalars().unique().all())

    async def _get_last_snapshot_date(self, db: AsyncSession) -> Optional[str]:
        result = await db.execute(
            select(SystemSetting.value).where(SystemSetting.key == LAST_SNAPSHOT_SETTING_KEY)
        )
        value = result.scalar_one_or_none()
        return value.strip() if value else None

    def _yesterday_moscow_date(self):
        now_moscow = datetime.now(MOSCOW_TZ)
        return now_moscow.date() - timedelta(days=1)

    def _build_remnawave_range(self, target_date) -> tuple[str, str]:
        start_local = datetime.combine(target_date, time.min, tzinfo=MOSCOW_TZ)
        end_local = datetime.combine(target_date, time.max, tzinfo=MOSCOW_TZ)
        start_utc = start_local.astimezone(UTC_TZ)
        end_utc = end_local.astimezone(UTC_TZ)
        return self._format_utc_for_remnawave(start_utc), self._format_utc_for_remnawave(end_utc)

    @staticmethod
    def _format_utc_for_remnawave(value: datetime) -> str:
        return value.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    def _extract_total_bytes(self, traffic_data: Dict[str, Any]) -> int:
        if not traffic_data:
            return 0

        response = traffic_data.get("response", traffic_data)

        if isinstance(response, list):
            return sum(self._safe_int(item.get("total")) for item in response if isinstance(item, dict))

        if isinstance(response, dict):
            for key in ("total", "totalBytes", "total_bytes", "traffic_bytes"):
                if key in response:
                    return self._safe_int(response.get(key))

            if "total_gb" in response:
                return self._gb_to_bytes(response.get("total_gb"))

            nodes = response.get("nodes")
            if isinstance(nodes, Iterable) and not isinstance(nodes, (str, bytes, dict)):
                total = 0
                for item in nodes:
                    if not isinstance(item, dict):
                        continue
                    if "total" in item:
                        total += self._safe_int(item.get("total"))
                    elif "gb" in item:
                        total += self._gb_to_bytes(item.get("gb"))
                return total

        return 0

    @staticmethod
    def _safe_int(value: Any) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _gb_to_bytes(value: Any) -> int:
        try:
            return max(0, int(float(value or 0) * (1024 ** 3)))
        except (TypeError, ValueError):
            return 0


user_daily_traffic_usage_service = UserDailyTrafficUsageService()
