import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import exists, select, text, update

from app.database.database import AsyncSessionLocal
from app.database.models import InteractiveNotificationLog, Subscription, User, UserStatus
from app.services.legacy_notify_once_service import LegacyNotifyOnceService


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _CampaignClaim:
    log_id: int
    recipients: list


class LegacyReviewBonusService:
    IS_ENABLED = False
    SLOT_KEY = "legacy_review_bonus"
    DAILY_LIMIT = 10_000
    SEND_DELAY_SECONDS = 0.04

    TEXT = (
        "<b>💡 +30 дней VPN — просто за отзыв!</b>\n\n"
        "1. Оцени приложение Leto VPN в Google Play на ⭐️⭐️⭐️⭐️⭐️ "
        "и напиши пару теплых слов\n"
        "2. Отправь скрин отзыва в бота @letosupportbot\n"
        "3. Получи +30 дней бесплатно"
    )
    BUTTON_TEXT = "Жми и забирай подарок 👇"
    BUTTON_URL = "https://play.google.com/store/apps/details?id=com.leto.split"

    def is_slot(self, slot_key: str) -> bool:
        return slot_key == self.SLOT_KEY

    async def run(self, bot: Optional[Bot]) -> None:
        if not self.IS_ENABLED:
            logger.info("Рассылка legacy review bonus отключена")
            return

        if bot is None:
            logger.error("Невозможно запустить legacy review bonus: бот не инициализирован")
            return

        run_date = datetime.now(timezone.utc).astimezone(
            LegacyNotifyOnceService.MSK_TZ
        ).date()
        claim = await self._claim_recipients(run_date)
        if claim is None or not claim.recipients:
            return

        counters = {"selected": len(claim.recipients), "sent": 0, "failed": 0}
        try:
            for recipient in claim.recipients:
                try:
                    message = await bot.send_message(
                        int(recipient.telegram_id),
                        self.TEXT,
                        reply_markup=self._keyboard(),
                        parse_mode="HTML",
                    )
                except Exception as exc:  # noqa: BLE001
                    counters["failed"] += 1
                    logger.error(
                        "Не удалось отправить legacy review bonus пользователю %s: %s",
                        recipient.telegram_id,
                        exc,
                    )
                    await self._record_delivery(
                        recipient.id,
                        status="failed",
                        error=str(exc),
                    )
                else:
                    counters["sent"] += 1
                    await self._record_delivery(
                        recipient.id,
                        status="sent",
                        message_id=message.message_id,
                    )
                await asyncio.sleep(self.SEND_DELAY_SECONDS)

            await self._finish_campaign(claim.log_id, status="processed", counters=counters)
        except asyncio.CancelledError:
            await self._finish_campaign(
                claim.log_id,
                status="failed",
                counters=counters,
                error="Campaign cancelled",
            )
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("Ошибка рассылки legacy review bonus: %s", exc)
            await self._finish_campaign(
                claim.log_id,
                status="failed",
                counters=counters,
                error=str(exc),
            )

    async def _claim_recipients(self, run_date: date) -> Optional[_CampaignClaim]:
        campaign_slot_key = self._campaign_slot_key(run_date)
        async with AsyncSessionLocal() as db:
            async with db.begin():
                # Reserve the daily batch atomically, before any Telegram requests are made.
                await db.execute(
                    text(
                        "LOCK TABLE interactive_notification_logs "
                        "IN SHARE ROW EXCLUSIVE MODE"
                    )
                )
                existing = await db.execute(
                    select(InteractiveNotificationLog.id)
                    .where(
                        InteractiveNotificationLog.slot_key == campaign_slot_key,
                        InteractiveNotificationLog.user_id.is_(None),
                    )
                    .limit(1)
                )
                if existing.scalar_one_or_none() is not None:
                    return None

                completed = await db.execute(
                    select(InteractiveNotificationLog.id)
                    .where(
                        InteractiveNotificationLog.slot_key == self._completion_slot_key(),
                        InteractiveNotificationLog.user_id.is_(None),
                    )
                    .limit(1)
                )
                if completed.scalar_one_or_none() is not None:
                    return None

                recipients = await self._list_recipients(db)
                campaign_log = InteractiveNotificationLog(
                    slot_key=campaign_slot_key,
                    status="running" if recipients else "processed",
                    payload={
                        "run_date": run_date.isoformat(),
                        "daily_limit": self.DAILY_LIMIT,
                        "selected": len(recipients),
                    },
                )
                db.add(campaign_log)

                if recipients:
                    db.add_all(
                        [
                            InteractiveNotificationLog(
                                slot_key=self.SLOT_KEY,
                                user_id=recipient.id,
                                telegram_id=recipient.telegram_id,
                                status="queued",
                                payload={"run_date": run_date.isoformat()},
                            )
                            for recipient in recipients
                        ]
                    )
                else:
                    db.add(
                        InteractiveNotificationLog(
                            slot_key=self._completion_slot_key(),
                            status="processed",
                            payload={
                                "completed_on": run_date.isoformat(),
                                "reason": "no_eligible_recipients",
                            },
                        )
                    )

                await db.flush()
                return _CampaignClaim(log_id=campaign_log.id, recipients=recipients)

    async def _list_recipients(self, db):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        already_attempted = exists(
            select(InteractiveNotificationLog.id).where(
                InteractiveNotificationLog.slot_key == self.SLOT_KEY,
                InteractiveNotificationLog.user_id == User.id,
            )
        )
        result = await db.execute(
            select(User.id, User.telegram_id)
            .outerjoin(Subscription, Subscription.user_id == User.id)
            .where(
                User.telegram_id.isnot(None),
                User.status == UserStatus.ACTIVE.value,
                LegacyNotifyOnceService._legacy_cohort_condition(),
                LegacyNotifyOnceService._inactive_subscription_condition(now),
                ~already_attempted,
            )
            .order_by(User.id.asc())
            .limit(self.DAILY_LIMIT)
        )
        return result.all()

    async def _record_delivery(
        self,
        user_id: int,
        *,
        status: str,
        message_id: Optional[int] = None,
        error: Optional[str] = None,
    ) -> None:
        async with AsyncSessionLocal() as db:
            await db.execute(
                update(InteractiveNotificationLog)
                .where(
                    InteractiveNotificationLog.slot_key == self.SLOT_KEY,
                    InteractiveNotificationLog.user_id == user_id,
                )
                .values(status=status, message_id=message_id, error=error)
            )
            await db.commit()

    async def _finish_campaign(
        self,
        log_id: int,
        *,
        status: str,
        counters: dict[str, int],
        error: Optional[str] = None,
    ) -> None:
        async with AsyncSessionLocal() as db:
            await db.execute(
                update(InteractiveNotificationLog)
                .where(InteractiveNotificationLog.id == log_id)
                .values(
                    status=status,
                    error=error,
                    payload={
                        "daily_limit": self.DAILY_LIMIT,
                        **counters,
                        "completed_at": datetime.now(timezone.utc)
                        .replace(tzinfo=None)
                        .isoformat(),
                    },
                )
            )
            await db.commit()

    def _keyboard(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=self.BUTTON_TEXT, url=self.BUTTON_URL)]
            ]
        )

    @classmethod
    def _campaign_slot_key(cls, run_date: date) -> str:
        return f"{cls.SLOT_KEY}:{run_date.isoformat()}"

    @classmethod
    def _completion_slot_key(cls) -> str:
        return f"{cls.SLOT_KEY}:complete"


legacy_review_bonus_service = LegacyReviewBonusService()
