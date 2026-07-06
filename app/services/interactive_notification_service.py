import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, time as datetime_time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database.database import AsyncSessionLocal
from app.database.models import InteractiveNotificationLog, Subscription, User
from app.services.cold_solo_offer_service import cold_solo_offer_service


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InteractiveNotificationSlot:
    key: str
    time: datetime_time


@dataclass(frozen=True)
class InteractiveNotificationResult:
    status: str
    user_id: Optional[int] = None
    telegram_id: Optional[int] = None
    message_id: Optional[int] = None
    error: Optional[str] = None
    payload: Optional[dict] = None


class InteractiveNotificationService:
    MSK_TZ = ZoneInfo("Europe/Moscow")
    SLOTS = (
        InteractiveNotificationSlot(cold_solo_offer_service.FIRST_SLOT_KEY, datetime_time(hour=10, minute=0)),
        InteractiveNotificationSlot(cold_solo_offer_service.SECOND_SLOT_KEY, datetime_time(hour=20, minute=0)),

        # TEST
        InteractiveNotificationSlot(cold_solo_offer_service.FIRST_SLOT_KEY, datetime_time(hour=10, minute=30)),
        InteractiveNotificationSlot(cold_solo_offer_service.SECOND_SLOT_KEY, datetime_time(hour=10, minute=40)),
    )
    BATCH_LIMIT = 500

    def __init__(self) -> None:
        self.bot: Optional[Bot] = None
        self._task: Optional[asyncio.Task] = None
        self._next_run: Optional[datetime] = None

    def set_bot(self, bot: Bot) -> None:
        self.bot = bot

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def get_next_run(self) -> Optional[datetime]:
        return self._next_run

    async def start(self) -> None:
        await self.stop()

        _, self._next_run = self._calculate_next_run()
        self._task = asyncio.create_task(self._run_loop())
        slots_text = ", ".join(
            f"{slot.key}={slot.time.strftime('%H:%M')}" for slot in self.SLOTS
        )
        logger.info("🔔 Сервис интерактивных уведомлений запущен: %s МСК", slots_text)

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        self._next_run = None

    async def _run_loop(self) -> None:
        try:
            while True:
                slot, next_run_utc = self._calculate_next_run()
                self._next_run = next_run_utc

                delay = (next_run_utc - datetime.now(timezone.utc)).total_seconds()
                if delay > 0:
                    await asyncio.sleep(delay)

                await self._process_slot(slot)

        except asyncio.CancelledError:
            logger.info("Сервис интерактивных уведомлений остановлен")
            raise
        finally:
            self._next_run = None

    def _calculate_next_run(self) -> tuple[InteractiveNotificationSlot, datetime]:
        now_msk = datetime.now(self.MSK_TZ)
        today = now_msk.date()

        for slot in sorted(self.SLOTS, key=lambda item: item.time):
            candidate_msk = datetime.combine(today, slot.time, tzinfo=self.MSK_TZ)
            if candidate_msk > now_msk:
                return slot, candidate_msk.astimezone(timezone.utc)

        first_slot = sorted(self.SLOTS, key=lambda item: item.time)[0]
        next_day = today + timedelta(days=1)
        candidate_msk = datetime.combine(next_day, first_slot.time, tzinfo=self.MSK_TZ)
        return first_slot, candidate_msk.astimezone(timezone.utc)

    async def _process_slot(self, slot: InteractiveNotificationSlot) -> None:
        payload = {
            "timezone": "Europe/Moscow",
            "slot_time": slot.time.strftime("%H:%M"),
        }

        try:
            result = await self._run_slot_logic(slot)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("Ошибка обработки интерактивного слота %s: %s", slot.key, exc)
            result = InteractiveNotificationResult(
                status="failed",
                error=str(exc),
                payload=payload,
            )

        result_payload = payload
        if result.payload:
            result_payload = {**payload, **result.payload}

        await self._record_log(
            slot,
            status=result.status,
            user_id=result.user_id,
            telegram_id=result.telegram_id,
            message_id=result.message_id,
            error=result.error,
            payload=result_payload,
        )

    async def _run_slot_logic(
        self,
        slot: InteractiveNotificationSlot,
    ) -> InteractiveNotificationResult:
        logger.info(
            "🔔 Наступил слот интерактивных уведомлений %s (%s МСК)",
            slot.key,
            slot.time.strftime("%H:%M"),
        )

        if slot.key == cold_solo_offer_service.FIRST_SLOT_KEY:
            return await self._send_cold_solo_first_touch(slot)

        if slot.key == cold_solo_offer_service.SECOND_SLOT_KEY:
            return await self._send_cold_solo_second_touch(slot)

        return InteractiveNotificationResult(status="processed")

    async def _send_cold_solo_first_touch(
        self,
        slot: InteractiveNotificationSlot,
    ) -> InteractiveNotificationResult:
        if not self.bot:
            return InteractiveNotificationResult(
                status="skipped",
                error="Bot instance is not available",
            )

        sent = 0
        failed = 0
        skipped = 0
        now = datetime.utcnow()
        cutoff = now - timedelta(hours=10)

        async with AsyncSessionLocal() as db:
            last_user_id = 0
            while True:
                result = await db.execute(
                    select(User)
                    .join(Subscription, Subscription.user_id == User.id)
                    .options(selectinload(User.subscription))
                    .where(
                        User.id > last_user_id,
                        User.telegram_id.isnot(None),
                        Subscription.is_trial.is_(True),
                        Subscription.end_date <= cutoff,
                    )
                    .order_by(User.id.asc())
                    .limit(self.BATCH_LIMIT)
                )
                users = list(result.scalars().all())
                if not users:
                    break

                last_user_id = max(user.id for user in users)

                for user in users:
                    if not await cold_solo_offer_service.is_initially_eligible(db, user, now_utc=now):
                        skipped += 1
                        continue

                    offer = await cold_solo_offer_service.ensure_offer(db, user, now_utc=now)
                    message_id = await self._send_cold_solo_message(
                        user.telegram_id,
                        first_touch=True,
                        offer_id=offer.id,
                    )
                    if message_id:
                        sent += 1
                        await self._record_log(
                            slot,
                            status="sent",
                            user_id=user.id,
                            telegram_id=user.telegram_id,
                            message_id=message_id,
                            payload={"offer_id": offer.id},
                        )
                    else:
                        failed += 1
                        await self._record_log(
                            slot,
                            status="failed",
                            user_id=user.id,
                            telegram_id=user.telegram_id,
                            error="send_message_failed",
                            payload={"offer_id": offer.id},
                        )

        return InteractiveNotificationResult(
            status="processed",
            payload={"sent": sent, "failed": failed, "skipped": skipped},
        )

    async def _send_cold_solo_second_touch(
        self,
        slot: InteractiveNotificationSlot,
    ) -> InteractiveNotificationResult:
        if not self.bot:
            return InteractiveNotificationResult(
                status="skipped",
                error="Bot instance is not available",
            )

        sent = 0
        failed = 0
        skipped = 0

        async with AsyncSessionLocal() as db:
            day_start, day_end = cold_solo_offer_service._today_utc_window()
            first_touch_user_ids = (
                select(InteractiveNotificationLog.user_id)
                .where(
                    InteractiveNotificationLog.slot_key == cold_solo_offer_service.FIRST_SLOT_KEY,
                    InteractiveNotificationLog.status == "sent",
                    InteractiveNotificationLog.created_at >= day_start,
                    InteractiveNotificationLog.created_at < day_end,
                )
                .distinct()
                .subquery()
            )

            last_user_id = 0
            while True:
                result = await db.execute(
                    select(User)
                    .join(first_touch_user_ids, first_touch_user_ids.c.user_id == User.id)
                    .options(selectinload(User.subscription))
                    .where(
                        User.id > last_user_id,
                        User.telegram_id.isnot(None),
                    )
                    .order_by(User.id.asc())
                    .limit(self.BATCH_LIMIT)
                )
                users = list(result.scalars().all())
                if not users:
                    break

                last_user_id = max(user.id for user in users)

                for user in users:
                    if await cold_solo_offer_service.was_touch_sent_today(db, user.id, slot.key):
                        skipped += 1
                        continue
                    if await cold_solo_offer_service.has_successful_subscription_purchase(db, user):
                        skipped += 1
                        continue
                    offer = await cold_solo_offer_service.get_active_offer(db, user.id)
                    if not offer:
                        skipped += 1
                        continue

                    message_id = await self._send_cold_solo_message(
                        user.telegram_id,
                        first_touch=False,
                        offer_id=offer.id,
                    )
                    if message_id:
                        sent += 1
                        await self._record_log(
                            slot,
                            status="sent",
                            user_id=user.id,
                            telegram_id=user.telegram_id,
                            message_id=message_id,
                            payload={"offer_id": offer.id},
                        )
                    else:
                        failed += 1
                        await self._record_log(
                            slot,
                            status="failed",
                            user_id=user.id,
                            telegram_id=user.telegram_id,
                            error="send_message_failed",
                            payload={"offer_id": offer.id},
                        )

        return InteractiveNotificationResult(
            status="processed",
            payload={"sent": sent, "failed": failed, "skipped": skipped},
        )

    async def _send_cold_solo_message(
        self,
        telegram_id: int,
        *,
        first_touch: bool,
        offer_id: int,
    ) -> Optional[int]:
        if not self.bot:
            return None

        if first_touch:
            text = (
                "<b>🔥 Особое предложение — только сегодня!</b>\n\n"
                "Триал закончился, а VPN все еще нужен?\n\n"
                "Забирай 1 год Solo за 990₽ вместо 1560₽\n"
                "(это 82₽/мес → дешевле чашки кофе)\n\n"
                "⏰ Это разовое предложение. Доступно до: 22:00 MSK."
            )
            button_text = "Забрать год Solo за 990₽"
        else:
            text = (
                "<b>⏰ Осталось 2 часа</b>\n\n"
                "1 Год Solo за 990₽ (82₽/мес) сгорит в 22:00.\n"
                "Второго такого предложения не будет."
            )
            button_text = "Забрать Solo за 990₽"

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=button_text,
                        callback_data=f"cold_solo_offer:claim:{offer_id}",
                    )
                ]
            ]
        )

        try:
            sent_message = await self.bot.send_message(
                telegram_id,
                text,
                reply_markup=keyboard,
                parse_mode="HTML",
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Не удалось отправить cold Solo offer пользователю %s: %s",
                telegram_id,
                exc,
            )
            return None

        return sent_message.message_id

    async def _record_log(
        self,
        slot: InteractiveNotificationSlot,
        *,
        status: str,
        user_id: Optional[int] = None,
        telegram_id: Optional[int] = None,
        message_id: Optional[int] = None,
        error: Optional[str] = None,
        payload: Optional[dict] = None,
    ) -> None:
        try:
            async with AsyncSessionLocal() as db:
                db.add(
                    InteractiveNotificationLog(
                        slot_key=slot.key,
                        user_id=user_id,
                        telegram_id=telegram_id,
                        message_id=message_id,
                        status=status,
                        error=error,
                        payload=payload,
                    )
                )
                await db.commit()
        except Exception as exc:  # noqa: BLE001
            logger.error("Не удалось записать лог интерактивного уведомления: %s", exc)


interactive_notification_service = InteractiveNotificationService()
