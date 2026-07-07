import logging
from datetime import datetime, time, timedelta, timezone
from typing import Callable, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.crud.notification import (
    get_latest_notification_sent_at,
)
from app.database.crud.system_setting import upsert_system_setting
from app.database.models import (
    SentNotification,
    Subscription,
    SubscriptionStatus,
    SystemSetting,
    User,
    UserDailyTrafficUsage,
    UserStatus as ModelUserStatus,
)


logger = logging.getLogger(__name__)


MOSCOW_TZ = ZoneInfo("Europe/Moscow")
UTC_TZ = timezone.utc

ANDROID_RATE_REQUEST_NOTIFICATION_TYPE = "android_rate_request"
ANDROID_RATE_REQUEST_LAST_RUN_KEY = "android_rate_request_last_run_date"
ANDROID_RATE_REQUEST_BATCH_SIZE = 500
ANDROID_RATE_REQUEST_TRAFFIC_THRESHOLD_BYTES = 20 * 1024 * 1024
ANDROID_RATE_REQUEST_REVIEW_URL = (
    "https://play.google.com/store/apps/details?id=com.leto.split&utm_source=letovpnbot"
)
ANDROID_RATE_REQUEST_CLICK_CALLBACK_PREFIX = "android_rate_request_click"
ANDROID_RATE_REQUEST_CLICK_PATH = "/android-rate-request/click"
IS_DEBUG = False
ANDROID_RATE_REQUEST_DEBUG_TELEGRAM_ID = 5708953214


def build_android_rate_request_callback_data(sent_notification_id: int) -> str:
    return f"{ANDROID_RATE_REQUEST_CLICK_CALLBACK_PREFIX}:{sent_notification_id}"


def build_android_rate_request_review_url(sent_notification_id: int) -> str:
    parts = urlsplit(ANDROID_RATE_REQUEST_REVIEW_URL)
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key != "sent_notification_id"
    ]
    query.append(("sent_notification_id", str(sent_notification_id)))
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


def build_android_rate_request_tracking_url(
    sent_notification_id: int,
    *,
    base_url: Optional[str] = None,
) -> str:
    configured_base_url = (base_url or settings.WEBHOOK_URL or "").strip().rstrip("/")
    if not configured_base_url:
        host = (
            settings.WEB_API_HOST if settings.WEB_API_HOST != "0.0.0.0" else "localhost"
        )
        configured_base_url = f"http://{host}:{settings.WEB_API_PORT}"

    query = urlencode({"sent_notification_id": str(sent_notification_id)})
    return f"{configured_base_url}{ANDROID_RATE_REQUEST_CLICK_PATH}?{query}"


class AndroidRateRequestService:
    def __init__(
        self,
        *,
        now_provider: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._now_provider = now_provider

    async def process_due_requests(self, db: AsyncSession, bot) -> dict:
        if not bot:
            return {"skipped": True, "reason": "bot_not_available"}

        now_moscow = self._now_moscow()
        if IS_DEBUG:
            logger.warning(
                "Android rate request debug mode enabled: only telegram_id=%s will be processed",
                ANDROID_RATE_REQUEST_DEBUG_TELEGRAM_ID,
            )

        if not IS_DEBUG and not self._is_send_window(now_moscow):
            return {"skipped": True, "reason": "outside_send_window"}

        run_date = now_moscow.date().isoformat()
        last_run_date = await self._get_last_run_date(db)
        if not IS_DEBUG and last_run_date == run_date:
            return {
                "skipped": True,
                "reason": "already_processed_today",
                "date": run_date,
            }

        total_candidates = 0
        sent = 0
        unreachable = 0
        skipped = 0
        failed = 0
        after_user_id = 0

        while True:
            subscriptions = await self._get_candidate_subscriptions(
                db,
                now_moscow,
                limit=ANDROID_RATE_REQUEST_BATCH_SIZE,
                after_user_id=after_user_id,
            )
            if not subscriptions:
                break

            total_candidates += len(subscriptions)
            batch_user_ids = [
                subscription.user.id
                for subscription in subscriptions
                if subscription.user
            ]
            if not batch_user_ids:
                skipped += len(subscriptions)
                break
            after_user_id = max(batch_user_ids)

            for subscription in subscriptions:
                user = subscription.user
                if not user or not user.telegram_id:
                    skipped += 1
                    continue

                notification = SentNotification(
                    user_id=user.id,
                    subscription_id=subscription.id,
                    notification_type=ANDROID_RATE_REQUEST_NOTIFICATION_TYPE,
                )

                try:
                    db.add(notification)
                    await db.flush()
                except Exception as error:
                    failed += 1
                    await db.rollback()
                    logger.error(
                        "Failed to prepare Android rate request notification for user %s: %s",
                        user.id,
                        error,
                    )
                    continue

                send_result = await self._send_rate_request(bot, user, notification.id)
                if send_result == "sent":
                    try:
                        await db.commit()
                        sent += 1
                    except Exception as error:
                        failed += 1
                        await db.rollback()
                        logger.error(
                            "Failed to record Android rate request notification for user %s: %s",
                            user.id,
                            error,
                        )
                elif send_result == "unreachable":
                    await db.rollback()
                    unreachable += 1
                else:
                    await db.rollback()
                    failed += 1

        await upsert_system_setting(
            db,
            ANDROID_RATE_REQUEST_LAST_RUN_KEY,
            run_date,
            "Last Moscow date when Android rate requests were processed",
        )
        await db.commit()

        result = {
            "skipped": False,
            "date": run_date,
            "candidates": total_candidates,
            "sent": sent,
            "unreachable": unreachable,
            "skipped_candidates": skipped,
            "failed": failed,
        }
        logger.info("Android rate request processing completed: %s", result)
        return result

    async def _get_candidate_subscriptions(
        self,
        db: AsyncSession,
        now_moscow: datetime,
        *,
        limit: int,
        after_user_id: int,
    ) -> list[Subscription]:
        now_utc_naive = self._to_utc_naive(now_moscow)
        if IS_DEBUG:
            result = await db.execute(
                select(Subscription)
                .join(User, User.id == Subscription.user_id)
                .options(selectinload(Subscription.user))
                .where(
                    and_(
                        User.telegram_id == ANDROID_RATE_REQUEST_DEBUG_TELEGRAM_ID,
                        User.id > after_user_id,
                    )
                )
                .order_by(User.id)
                .limit(1)
            )
            subscriptions = list(result.scalars().unique().all())
            if not subscriptions and after_user_id == 0:
                logger.warning(
                    "Android rate request debug candidate not found for telegram_id=%s",
                    ANDROID_RATE_REQUEST_DEBUG_TELEGRAM_ID,
                )
            return subscriptions

        minimum_start_date = now_utc_naive - timedelta(days=3)
        cooldown_boundary = now_utc_naive - timedelta(days=30)
        required_dates = [
            now_moscow.date() - timedelta(days=days_ago) for days_ago in (1, 2, 3)
        ]

        traffic_qualified_users = (
            select(UserDailyTrafficUsage.user_id.label("user_id"))
            .where(
                UserDailyTrafficUsage.date.in_(required_dates),
                UserDailyTrafficUsage.traffic_bytes
                >= ANDROID_RATE_REQUEST_TRAFFIC_THRESHOLD_BYTES,
            )
            .group_by(UserDailyTrafficUsage.user_id)
            .having(func.count(func.distinct(UserDailyTrafficUsage.date)) == 3)
            .subquery()
        )

        latest_rate_requests = (
            select(
                SentNotification.user_id.label("user_id"),
                func.max(SentNotification.created_at).label("latest_sent_at"),
            )
            .where(
                SentNotification.notification_type
                == ANDROID_RATE_REQUEST_NOTIFICATION_TYPE,
                SentNotification.created_at < now_utc_naive,
            )
            .group_by(SentNotification.user_id)
            .subquery()
        )

        result = await db.execute(
            select(Subscription)
            .join(User, User.id == Subscription.user_id)
            .join(traffic_qualified_users, traffic_qualified_users.c.user_id == User.id)
            .outerjoin(latest_rate_requests, latest_rate_requests.c.user_id == User.id)
            .options(selectinload(Subscription.user))
            .where(
                and_(
                    User.telegram_id.isnot(None),
                    User.has_used_mobile_app == True,  # noqa: E712
                    User.id > after_user_id,
                    User.status == ModelUserStatus.ACTIVE.value,
                    Subscription.status == SubscriptionStatus.ACTIVE.value,
                    Subscription.is_trial == False,  # noqa: E712
                    Subscription.start_date.isnot(None),
                    Subscription.start_date <= minimum_start_date,
                    Subscription.end_date > now_utc_naive,
                    or_(
                        latest_rate_requests.c.latest_sent_at.is_(None),
                        latest_rate_requests.c.latest_sent_at <= cooldown_boundary,
                    ),
                )
            )
            .order_by(User.id)
            .limit(limit)
        )
        return list(result.scalars().unique().all())

    async def _is_in_cooldown(
        self,
        db: AsyncSession,
        user_id: int,
        now_moscow: datetime,
    ) -> bool:
        latest_sent_at = await get_latest_notification_sent_at(
            db,
            user_id,
            ANDROID_RATE_REQUEST_NOTIFICATION_TYPE,
        )
        if latest_sent_at is None:
            return False

        latest_sent_at = self._normalize_db_datetime(latest_sent_at)
        cooldown_boundary = self._to_utc_naive(now_moscow) - timedelta(days=30)
        return latest_sent_at > cooldown_boundary

    async def _has_required_traffic(
        self,
        db: AsyncSession,
        user_id: int,
        now_moscow: datetime,
    ) -> bool:
        required_dates = [
            now_moscow.date() - timedelta(days=offset) for offset in (1, 2, 3)
        ]

        result = await db.execute(
            select(
                UserDailyTrafficUsage.date, UserDailyTrafficUsage.traffic_bytes
            ).where(
                UserDailyTrafficUsage.user_id == user_id,
                UserDailyTrafficUsage.date.in_(required_dates),
            )
        )
        usage_by_date = {row[0]: int(row[1] or 0) for row in result.all()}

        return all(
            usage_by_date.get(usage_date, 0)
            >= ANDROID_RATE_REQUEST_TRAFFIC_THRESHOLD_BYTES
            for usage_date in required_dates
        )

    async def _get_last_run_date(self, db: AsyncSession) -> Optional[str]:
        result = await db.execute(
            select(SystemSetting.value).where(
                SystemSetting.key == ANDROID_RATE_REQUEST_LAST_RUN_KEY
            )
        )
        value = result.scalar_one_or_none()
        return value.strip() if value else None

    async def _send_rate_request(
        self, bot, user: User, sent_notification_id: int
    ) -> str:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⭐ Оставить отзыв",
                        url=build_android_rate_request_tracking_url(
                            sent_notification_id
                        ),
                    )
                ]
            ]
        )
        message = (
            "<b>Спасибо, что пользуешься Leto VPN</b> ❤️\n\n"
            "Если сервис помогает тебе оставаться на связи, пожалуйста, оцени приложение "
            "в Google Play. Это помогает нам расти и делать VPN лучше."
        )

        try:
            await bot.send_message(
                chat_id=user.telegram_id,
                text=message,
                reply_markup=keyboard,
                parse_mode="HTML",
            )
            return "sent"
        except TelegramForbiddenError:
            logger.warning(
                "Android rate request recipient %s is unreachable: bot blocked",
                user.telegram_id,
            )
            return "unreachable"
        except TelegramBadRequest as error:
            if self._is_unreachable_error(error):
                logger.warning(
                    "Android rate request recipient %s is unreachable: %s",
                    user.telegram_id,
                    error,
                )
                return "unreachable"

            logger.error(
                "Failed to send Android rate request to %s: %s",
                user.telegram_id,
                error,
            )
            return "failed"
        except Exception as error:
            logger.error(
                "Failed to send Android rate request to %s: %s",
                user.telegram_id,
                error,
            )
            return "failed"

    def _now_moscow(self) -> datetime:
        if self._now_provider is None:
            return datetime.now(MOSCOW_TZ)

        value = self._now_provider()
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC_TZ)
        return value.astimezone(MOSCOW_TZ)

    @staticmethod
    def _is_send_window(value: datetime) -> bool:
        current_time = value.timetz().replace(tzinfo=None)
        # return True
        return time(20, 0) <= current_time < time(22, 0)

    @staticmethod
    def _to_utc_naive(value: datetime) -> datetime:
        aware_value = (
            value if value.tzinfo is not None else value.replace(tzinfo=UTC_TZ)
        )
        return aware_value.astimezone(UTC_TZ).replace(tzinfo=None)

    @staticmethod
    def _normalize_db_datetime(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value
        return value.astimezone(UTC_TZ).replace(tzinfo=None)

    @staticmethod
    def _is_unreachable_error(error: TelegramBadRequest) -> bool:
        message = str(error).lower()
        unreachable_markers = (
            "chat not found",
            "user is deactivated",
            "bot was blocked by the user",
            "bot can't initiate conversation",
            "can't initiate conversation",
            "user not found",
            "peer id invalid",
        )
        return any(marker in message for marker in unreachable_markers)


android_rate_request_service = AndroidRateRequestService()
