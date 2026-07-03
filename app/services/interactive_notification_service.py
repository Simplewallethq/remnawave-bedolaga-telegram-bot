import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, time as datetime_time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from aiogram import Bot

from app.database.crud.user import get_user_by_username
from app.database.database import AsyncSessionLocal
from app.database.models import InteractiveNotificationLog


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
        InteractiveNotificationSlot("a_1", datetime_time(hour=10, minute=0)),
        InteractiveNotificationSlot("test", datetime_time(hour=12, minute=30)),
        InteractiveNotificationSlot("a_2", datetime_time(hour=20, minute=0)),
    )
    TEST_USERNAME = "cheechgodx"
    TEST_MESSAGE = "This is test message"

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

        if slot.key == "test":
            return await self._send_test_message()

        return InteractiveNotificationResult(status="processed")

    async def _send_test_message(self) -> InteractiveNotificationResult:
        if not self.bot:
            return InteractiveNotificationResult(
                status="skipped",
                error="Bot instance is not available",
                payload={"target_username": self.TEST_USERNAME},
            )

        async with AsyncSessionLocal() as db:
            user = await get_user_by_username(db, self.TEST_USERNAME)

        if not user:
            return InteractiveNotificationResult(
                status="skipped",
                error=f"User @{self.TEST_USERNAME} not found",
                payload={"target_username": self.TEST_USERNAME},
            )

        if not user.telegram_id:
            return InteractiveNotificationResult(
                status="skipped",
                user_id=user.id,
                error=f"User @{self.TEST_USERNAME} has no telegram_id",
                payload={"target_username": self.TEST_USERNAME},
            )

        try:
            sent_message = await self.bot.send_message(
                user.telegram_id,
                self.TEST_MESSAGE,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Не удалось отправить тестовое интерактивное уведомление @%s: %s",
                self.TEST_USERNAME,
                exc,
            )
            return InteractiveNotificationResult(
                status="failed",
                user_id=user.id,
                telegram_id=user.telegram_id,
                error=str(exc),
                payload={"target_username": self.TEST_USERNAME},
            )

        logger.info(
            "✅ Тестовое интерактивное уведомление отправлено @%s",
            self.TEST_USERNAME,
        )
        return InteractiveNotificationResult(
            status="sent",
            user_id=user.id,
            telegram_id=user.telegram_id,
            message_id=sent_message.message_id,
            payload={"target_username": self.TEST_USERNAME},
        )

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
