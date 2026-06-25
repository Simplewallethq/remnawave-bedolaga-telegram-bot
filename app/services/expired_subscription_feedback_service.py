import html
import logging
from datetime import datetime, time, timedelta, timezone
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.crud.feedback import (
    create_feedback,
    get_feedback_by_event_key,
    set_feedback_message_id,
    set_feedback_status,
)
from app.database.crud.system_setting import upsert_system_setting
from app.database.models import (
    Feedback,
    Subscription,
    SubscriptionStatus as ModelSubscriptionStatus,
    SystemSetting,
    User,
    UserStatus as ModelUserStatus,
)


logger = logging.getLogger(__name__)


MOSCOW_TZ = ZoneInfo("Europe/Moscow")
UTC_TZ = timezone.utc

EXPIRED_SUBSCRIPTION_FEEDBACK_TYPE = "expired_subscription"
EXPIRED_SUBSCRIPTION_FEEDBACK_LAST_RUN_KEY = "expired_subscription_feedback_last_run_date"
EXPIRED_SUBSCRIPTION_FEEDBACK_BATCH_SIZE = 500

FEEDBACK_STATUS_SENT = "sent"
FEEDBACK_STATUS_WAITING_FOR_ANSWER = "waiting_for_answer"
FEEDBACK_STATUS_COMPLETED = "completed"
FEEDBACK_STATUS_FAILED = "failed"

OPTION_EXPENSIVE = "expensive"
OPTION_PAYMENT_FAILED = "payment_failed"
OPTION_BAD_SERVICE = "bad_service"
OPTION_SWITCHED_SERVICE = "switched_service"
OPTION_NOT_NEEDED = "not_needed"

EXPIRED_SUBSCRIPTION_FEEDBACK_OPTIONS = {
    OPTION_EXPENSIVE: "Дорого",
    OPTION_PAYMENT_FAILED: "Не получилось оплатить",
    OPTION_BAD_SERVICE: "Не работал как надо",
    OPTION_SWITCHED_SERVICE: "Ушёл в другой сервис",
    OPTION_NOT_NEEDED: "Больше не нужен",
}

EXPIRED_SUBSCRIPTION_FOLLOWUP_QUESTIONS = {
    OPTION_EXPENSIVE: "А какая цена была бы для тебя комфортной?",
    OPTION_PAYMENT_FAILED: "Расскажи, пожалуйста, что именно не получилось?",
    OPTION_BAD_SERVICE: "А что именно — скорость, обрывы или не открывался нужный сервис?",
    OPTION_SWITCHED_SERVICE: "Подскажи, к какому сервису ушёл и что в нём оказалось лучше?",
}

EXPIRED_SUBSCRIPTION_FEEDBACK_CALLBACK_PREFIX = "feedback_expired"


class ExpiredSubscriptionFeedbackService:
    def __init__(self, *, now_provider: Optional[Callable[[], datetime]] = None) -> None:
        self._now_provider = now_provider

    async def process_due_feedbacks(self, db: AsyncSession, bot) -> dict:
        if not bot:
            return {"skipped": True, "reason": "bot_not_available"}

        now_moscow = self._now_moscow()
        if not self._is_send_window(now_moscow):
            return {"skipped": True, "reason": "outside_send_window"}

        run_date = now_moscow.date().isoformat()
        last_run_date = await self._get_last_run_date(db)
        if last_run_date == run_date:
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
                limit=EXPIRED_SUBSCRIPTION_FEEDBACK_BATCH_SIZE,
                after_user_id=after_user_id,
            )
            if not subscriptions:
                break

            total_candidates += len(subscriptions)
            batch_user_ids = [subscription.user.id for subscription in subscriptions if subscription.user]
            if not batch_user_ids:
                skipped += len(subscriptions)
                break
            after_user_id = max(batch_user_ids)

            for subscription in subscriptions:
                user = subscription.user
                if not user or not user.telegram_id:
                    skipped += 1
                    continue

                ended_on_msk = self._to_moscow_date(subscription.end_date)
                event_key = self.build_event_key(user.id, ended_on_msk.isoformat())
                existing_feedback = await get_feedback_by_event_key(db, event_key)
                if existing_feedback:
                    skipped += 1
                    continue

                try:
                    feedback = await create_feedback(
                        db,
                        feedback_type=EXPIRED_SUBSCRIPTION_FEEDBACK_TYPE,
                        event_key=event_key,
                        user_id=user.id,
                        subscription_id=subscription.id,
                        status=FEEDBACK_STATUS_SENT,
                        context={"ended_on_msk": ended_on_msk.isoformat()},
                    )
                    await db.commit()
                except IntegrityError:
                    await db.rollback()
                    skipped += 1
                    continue

                send_result = await self._send_feedback_request(bot, user, feedback, ended_on_msk)
                if send_result["status"] == "sent":
                    await set_feedback_message_id(db, feedback, send_result["message_id"])
                    await db.commit()
                    sent += 1
                elif send_result["status"] == "unreachable":
                    await set_feedback_status(db, feedback, FEEDBACK_STATUS_FAILED)
                    await db.commit()
                    unreachable += 1
                else:
                    await set_feedback_status(db, feedback, FEEDBACK_STATUS_FAILED)
                    await db.commit()
                    failed += 1

        await upsert_system_setting(
            db,
            EXPIRED_SUBSCRIPTION_FEEDBACK_LAST_RUN_KEY,
            run_date,
            "Last Moscow date when expired subscription feedbacks were processed",
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
        logger.info("Expired subscription feedback processing completed: %s", result)
        return result

    async def _get_candidate_subscriptions(
        self,
        db: AsyncSession,
        now_moscow: datetime,
        *,
        limit: int,
        after_user_id: int,
    ) -> list[Subscription]:
        start_utc_naive, end_utc_naive = self._previous_two_msk_days_range(now_moscow)

        result = await db.execute(
            select(Subscription)
            .join(User, User.id == Subscription.user_id)
            .options(selectinload(Subscription.user))
            .where(
                and_(
                    User.telegram_id.isnot(None),
                    User.id > after_user_id,
                    User.status == ModelUserStatus.ACTIVE.value,
                    Subscription.status == ModelSubscriptionStatus.EXPIRED.value,
                    Subscription.is_trial == False,  # noqa: E712
                    Subscription.is_partner == False,  # noqa: E712
                    Subscription.end_date >= start_utc_naive,
                    Subscription.end_date < end_utc_naive,
                )
            )
            .order_by(User.id)
            .limit(limit)
        )
        return list(result.scalars().unique().all())

    async def _get_last_run_date(self, db: AsyncSession) -> Optional[str]:
        result = await db.execute(
            select(SystemSetting.value).where(
                SystemSetting.key == EXPIRED_SUBSCRIPTION_FEEDBACK_LAST_RUN_KEY
            )
        )
        value = result.scalar_one_or_none()
        return value.strip() if value else None

    async def _send_feedback_request(
        self,
        bot,
        user: User,
        feedback: Feedback,
        ended_on_msk,
    ) -> dict:
        try:
            sent_message = await bot.send_message(
                chat_id=user.telegram_id,
                text=self._build_message_text(user, ended_on_msk),
                reply_markup=self._build_keyboard(feedback.id),
                parse_mode="HTML",
            )
            return {"status": "sent", "message_id": sent_message.message_id}
        except TelegramForbiddenError:
            logger.warning(
                "Expired subscription feedback recipient %s is unreachable: bot blocked",
                user.telegram_id,
            )
            return {"status": "unreachable"}
        except TelegramBadRequest as error:
            if self._is_unreachable_error(error):
                logger.warning(
                    "Expired subscription feedback recipient %s is unreachable: %s",
                    user.telegram_id,
                    error,
                )
                return {"status": "unreachable"}

            logger.error(
                "Failed to send expired subscription feedback to %s: %s",
                user.telegram_id,
                error,
            )
            return {"status": "failed"}
        except Exception as error:
            logger.error(
                "Failed to send expired subscription feedback to %s: %s",
                user.telegram_id,
                error,
            )
            return {"status": "failed"}

    def _build_keyboard(self, feedback_id: int) -> InlineKeyboardMarkup:
        buttons = []
        for option, label in EXPIRED_SUBSCRIPTION_FEEDBACK_OPTIONS.items():
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=label,
                        callback_data=(
                            f"{EXPIRED_SUBSCRIPTION_FEEDBACK_CALLBACK_PREFIX}:"
                            f"{feedback_id}:{option}"
                        ),
                    )
                ]
            )
        return InlineKeyboardMarkup(inline_keyboard=buttons)

    def _build_message_text(self, user: User, ended_on_msk) -> str:
        name = self._format_user_name(user)
        ended_phrase = self._format_ended_phrase(ended_on_msk)
        return (
            f"Привет, {name}! Твоя подписка на Leto VPN закончилась {ended_phrase}, "
            "но ты её не продлил. Подскажешь, что помешало? "
            "Нам важно это понять, чтобы стать лучше."
        )

    def _format_ended_phrase(self, ended_on_msk) -> str:
        today_msk = self._now_moscow().date()
        if ended_on_msk == today_msk - timedelta(days=1):
            return "вчера"
        if ended_on_msk == today_msk - timedelta(days=2):
            return "позавчера"
        return "недавно"

    @staticmethod
    def _format_user_name(user: User) -> str:
        if user.username:
            return f"@{html.escape(user.username)}"
        return html.escape(user.full_name)

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
    def _previous_two_msk_days_range(value: datetime) -> tuple[datetime, datetime]:
        today_start_msk = datetime.combine(value.date(), time.min, tzinfo=MOSCOW_TZ)
        start_msk = today_start_msk - timedelta(days=2)
        return (
            start_msk.astimezone(UTC_TZ).replace(tzinfo=None),
            today_start_msk.astimezone(UTC_TZ).replace(tzinfo=None),
        )

    @staticmethod
    def _to_moscow_date(value: datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC_TZ)
        return value.astimezone(MOSCOW_TZ).date()

    @staticmethod
    def build_event_key(user_id: int, ended_on_msk: str) -> str:
        return f"{EXPIRED_SUBSCRIPTION_FEEDBACK_TYPE}:{user_id}:{ended_on_msk}"

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


expired_subscription_feedback_service = ExpiredSubscriptionFeedbackService()
