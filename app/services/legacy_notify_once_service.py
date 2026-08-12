import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import and_, func, or_, select, text, update

from app.config import settings
from app.database.database import AsyncSessionLocal
from app.database.models import (
    InteractiveNotificationLog,
    Subscription,
    SubscriptionStatus,
    User,
    UserStatus,
)


logger = logging.getLogger(__name__)


class LegacyNotifyOnceService:
    SLOT_KEY = "legacy_notify_once"
    MSK_TZ = ZoneInfo("Europe/Moscow")
    RUN_AT = datetime(2026, 8, 11, 0, 0, tzinfo=MSK_TZ)
    BATCH_LIMIT = 500
    SEND_DELAY_SECONDS = 0.04

    TEXT = (
        "<b>👍 Хорошие новости о ценах</b>\n\n"
        "Мы услышали вас — пересчитали и сделали дешевле:\n"
        "Solo теперь 220₽ в месяц, а не 320₽.\n\n"
        "Спасибо, что говорите нам, когда что-то не так. Твоя подписка ждёт."
    )
    BUTTON_TEXT = "Продлить за 220₽"

    def is_slot(self, slot_key: str) -> bool:
        return slot_key == self.SLOT_KEY

    async def is_terminal(self) -> bool:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(InteractiveNotificationLog.id)
                .where(
                    InteractiveNotificationLog.slot_key == self.SLOT_KEY,
                )
                .limit(1)
            )
            return result.scalar_one_or_none() is not None

    async def run(self, bot: Optional[Bot]) -> None:
        log_id = await self._start_campaign()
        if log_id is None:
            return

        counters = {"selected": 0, "sent": 0, "failed": 0, "skipped": 0, "batches": 0}
        try:
            if bot is None:
                await self._finish_campaign(
                    log_id,
                    status="skipped",
                    counters=counters,
                    error="Bot instance is not available",
                )
                return

            last_user_id = 0
            while True:
                recipients = await self._list_recipients(last_user_id)
                if not recipients:
                    break

                counters["batches"] += 1
                counters["selected"] += len(recipients)
                last_user_id = recipients[-1].id

                for recipient in recipients:
                    try:
                        await bot.send_message(
                            int(recipient.telegram_id),
                            self.TEXT,
                            reply_markup=self._keyboard(),
                            parse_mode="HTML",
                        )
                    except Exception as exc:  # noqa: BLE001
                        counters["failed"] += 1
                        logger.error(
                            "Не удалось отправить legacy price notification пользователю %s: %s",
                            recipient.telegram_id,
                            exc,
                        )
                    else:
                        counters["sent"] += 1
                    await asyncio.sleep(self.SEND_DELAY_SECONDS)

            await self._finish_campaign(log_id, status="processed", counters=counters)
        except asyncio.CancelledError:
            # A running record is terminal by design: a restart must not duplicate the campaign.
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("Ошибка рассылки legacy price notification: %s", exc)
            await self._finish_campaign(
                log_id,
                status="failed",
                counters=counters,
                error=str(exc),
            )

    async def _list_recipients(self, last_user_id: int):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(User.id, User.telegram_id)
                .outerjoin(Subscription, Subscription.user_id == User.id)
                .where(
                    User.id > last_user_id,
                    User.telegram_id.isnot(None),
                    User.status == UserStatus.ACTIVE.value,
                    self._legacy_cohort_condition(),
                    self._inactive_subscription_condition(now),
                )
                .order_by(User.id.asc())
                .limit(self.BATCH_LIMIT)
            )
            return result.all()

    @staticmethod
    def _inactive_subscription_condition(now: datetime):
        return or_(
            Subscription.id.is_(None),
            Subscription.status.is_(None),
            Subscription.status != SubscriptionStatus.ACTIVE.value,
            Subscription.end_date.is_(None),
            Subscription.end_date <= now,
        )

    @staticmethod
    def _legacy_cohort_condition():
        override = func.lower(User.tariff_pricing_cohort_override)
        valid_override = override.in_(("legacy", "new"))
        fallback_override = or_(
            User.tariff_pricing_cohort_override.is_(None),
            ~valid_override,
        )
        cutoff = settings.get_tariffs_new_pricing_cutoff()

        if cutoff is None:
            fallback_cohort = fallback_override
        else:
            fallback_cohort = and_(
                fallback_override,
                or_(User.created_at.is_(None), User.created_at < cutoff),
            )

        return or_(override == "legacy", fallback_cohort)

    async def _start_campaign(self) -> Optional[int]:
        async with AsyncSessionLocal() as db:
            async with db.begin():
                await db.execute(
                    text(
                        "LOCK TABLE interactive_notification_logs "
                        "IN SHARE ROW EXCLUSIVE MODE"
                    )
                )
                existing = await db.execute(
                    select(InteractiveNotificationLog.id)
                    .where(
                        InteractiveNotificationLog.slot_key == self.SLOT_KEY,
                        InteractiveNotificationLog.user_id.is_(None),
                    )
                    .limit(1)
                )
                if existing.scalar_one_or_none() is not None:
                    return None

                log = InteractiveNotificationLog(
                    slot_key=self.SLOT_KEY,
                    status="running",
                    payload={
                        "timezone": "Europe/Moscow",
                        "batch_size": self.BATCH_LIMIT,
                    },
                )
                db.add(log)
            await db.refresh(log)
            return log.id

    async def _finish_campaign(
        self,
        log_id: int,
        *,
        status: str,
        counters: dict[str, int],
        error: Optional[str] = None,
    ) -> None:
        payload = {
            "timezone": "Europe/Moscow",
            "batch_size": self.BATCH_LIMIT,
            **counters,
            "completed_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        }
        async with AsyncSessionLocal() as db:
            await db.execute(
                update(InteractiveNotificationLog)
                .where(InteractiveNotificationLog.id == log_id)
                .values(status=status, error=error, payload=payload)
            )
            await db.commit()

    def _keyboard(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=self.BUTTON_TEXT,
                        callback_data="subscription_extend",
                    )
                ]
            ]
        )


legacy_notify_once_service = LegacyNotifyOnceService()
