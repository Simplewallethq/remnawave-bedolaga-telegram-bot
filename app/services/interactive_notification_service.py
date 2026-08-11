import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, time as datetime_time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.database.database import AsyncSessionLocal
from app.database.models import InteractiveNotificationLog, Subscription, User
from app.services.cold_solo_offer_service import cold_solo_offer_service
from app.services.hot_invoice_offer_service import (
    HotInvoiceCandidate,
    hot_invoice_offer_service,
)
from app.services.expired_subscription_offer_service import (
    ExpiredSubscriptionCandidate,
    expired_subscription_offer_service,
)
from app.services.legacy_pro_offer_service import legacy_pro_offer_service
from app.services.legacy_notify_once_service import legacy_notify_once_service
from app.services.vpn_deposit_bonus_service import vpn_deposit_bonus_service


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InteractiveNotificationSlot:
    key: str
    time: Optional[datetime_time] = None
    run_at: Optional[datetime] = None
    interval: Optional[timedelta] = None

    @property
    def is_one_off(self) -> bool:
        return self.run_at is not None

    @property
    def is_interval(self) -> bool:
        return self.interval is not None


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
        InteractiveNotificationSlot(
            hot_invoice_offer_service.FIRST_SLOT_KEY,
            interval=timedelta(minutes=1),
        ),
        InteractiveNotificationSlot(
            hot_invoice_offer_service.SECOND_SLOT_KEY,
            datetime_time(hour=10, minute=0),
        ),
        InteractiveNotificationSlot(
            hot_invoice_offer_service.THIRD_SLOT_KEY,
            datetime_time(hour=21, minute=0),
        ),
        InteractiveNotificationSlot(
            hot_invoice_offer_service.FOURTH_MORNING_SLOT_KEY,
            datetime_time(hour=10, minute=0),
        ),
        InteractiveNotificationSlot(
            hot_invoice_offer_service.FOURTH_EVENING_SLOT_KEY,
            datetime_time(hour=21, minute=0),
        ),
        InteractiveNotificationSlot(
            expired_subscription_offer_service.FIRST_SLOT_KEY,
            interval=timedelta(minutes=1),
        ),
        InteractiveNotificationSlot(
            expired_subscription_offer_service.SECOND_SLOT_KEY,
            datetime_time(hour=10, minute=0),
        ),
        InteractiveNotificationSlot(
            expired_subscription_offer_service.THIRD_SLOT_KEY,
            datetime_time(hour=21, minute=0),
        ),
        InteractiveNotificationSlot(
            expired_subscription_offer_service.FOURTH_MORNING_SLOT_KEY,
            datetime_time(hour=10, minute=0),
        ),
        InteractiveNotificationSlot(
            expired_subscription_offer_service.FOURTH_EVENING_SLOT_KEY,
            datetime_time(hour=21, minute=0),
        ),
        InteractiveNotificationSlot(
            vpn_deposit_bonus_service.TOUCH_1_SLOT,
            interval=timedelta(minutes=1),
        ),
        InteractiveNotificationSlot(
            vpn_deposit_bonus_service.TOUCH_2_SLOT,
            datetime_time(hour=22, minute=0),
        ),
        *(
            InteractiveNotificationSlot(key, run_at.timetz().replace(tzinfo=None), run_at)
            for key, run_at in legacy_pro_offer_service.get_one_off_schedule()
        ),
        InteractiveNotificationSlot(
            legacy_notify_once_service.SLOT_KEY,
            run_at=legacy_notify_once_service.RUN_AT,
        ),
    )
    BATCH_LIMIT = 500

    def __init__(self) -> None:
        self.bot: Optional[Bot] = None
        self._task: Optional[asyncio.Task] = None
        self._debug_task: Optional[asyncio.Task] = None
        self._expired_debug_task: Optional[asyncio.Task] = None
        self._next_run: Optional[datetime] = None

    def set_bot(self, bot: Bot) -> None:
        self.bot = bot

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def get_next_run(self) -> Optional[datetime]:
        return self._next_run

    async def start(self) -> None:
        await self.stop()

        _, self._next_run = await self._calculate_next_run()
        self._task = asyncio.create_task(self._run_loop())
        if hot_invoice_offer_service.is_debug_enabled():
            self._debug_task = asyncio.create_task(self._run_hot_invoice_debug_sequence())
        if expired_subscription_offer_service.is_debug_enabled():
            self._expired_debug_task = asyncio.create_task(
                self._run_expired_subscription_debug_sequence()
            )
        slots_text = ", ".join(
            self._format_slot_for_log(slot) for slot in self.SLOTS
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
        if self._debug_task and not self._debug_task.done():
            self._debug_task.cancel()
            try:
                await self._debug_task
            except asyncio.CancelledError:
                pass
        self._debug_task = None
        if self._expired_debug_task and not self._expired_debug_task.done():
            self._expired_debug_task.cancel()
            try:
                await self._expired_debug_task
            except asyncio.CancelledError:
                pass
        self._expired_debug_task = None
        self._next_run = None

    async def _run_loop(self) -> None:
        try:
            while True:
                slots, next_run_utc = await self._calculate_next_run()
                self._next_run = next_run_utc

                delay = (next_run_utc - datetime.now(timezone.utc)).total_seconds()
                if delay > 0:
                    await asyncio.sleep(delay)

                for slot in slots:
                    await self._process_slot(slot)

        except asyncio.CancelledError:
            logger.info("Сервис интерактивных уведомлений остановлен")
            raise
        finally:
            self._next_run = None

    async def _calculate_next_run(self) -> tuple[list[InteractiveNotificationSlot], datetime]:
        now_msk = datetime.now(self.MSK_TZ)
        now_utc = datetime.now(timezone.utc)
        today = now_msk.date()
        candidates: list[tuple[InteractiveNotificationSlot, datetime]] = []
        due_one_off_candidates: list[tuple[InteractiveNotificationSlot, datetime]] = []

        for slot in self.SLOTS:
            if slot.is_interval:
                candidates.append((slot, now_utc + slot.interval))
                continue
            if slot.is_one_off:
                if await self._is_one_off_slot_processed(slot):
                    continue
                scheduled_msk = self._slot_run_at_msk(slot)
                if scheduled_msk <= now_msk:
                    due_one_off_candidates.append((slot, scheduled_msk))
                    continue
                scheduled_utc = scheduled_msk.astimezone(timezone.utc)
                candidates.append((slot, scheduled_utc))
                continue

            if slot.time is None:
                continue
            candidate_msk = datetime.combine(today, slot.time, tzinfo=self.MSK_TZ)
            if candidate_msk > now_msk:
                candidates.append((slot, candidate_msk.astimezone(timezone.utc)))
                continue

            next_day = today + timedelta(days=1)
            next_candidate_msk = datetime.combine(next_day, slot.time, tzinfo=self.MSK_TZ)
            candidates.append((slot, next_candidate_msk.astimezone(timezone.utc)))

        if due_one_off_candidates:
            latest_due_at = max(item[1] for item in due_one_off_candidates)
            latest_due_slots = [
                slot for slot, scheduled_msk in due_one_off_candidates
                if scheduled_msk == latest_due_at
            ]
            return latest_due_slots, now_utc

        if not candidates:
            recurring_slots = [
                slot for slot in self.SLOTS
                if not slot.is_one_off and not slot.is_interval and slot.time is not None
            ]
            first_slot = sorted(recurring_slots, key=lambda item: item.time)[0]
            next_day = today + timedelta(days=1)
            candidate_msk = datetime.combine(next_day, first_slot.time, tzinfo=self.MSK_TZ)
            return [first_slot], candidate_msk.astimezone(timezone.utc)

        next_run = min(item[1] for item in candidates)
        due_slots = [slot for slot, candidate in candidates if candidate == next_run]
        return due_slots, next_run

    def _slot_run_at_msk(self, slot: InteractiveNotificationSlot) -> datetime:
        if slot.run_at is None:
            return datetime.combine(datetime.now(self.MSK_TZ).date(), slot.time, tzinfo=self.MSK_TZ)
        if slot.run_at.tzinfo is None:
            return slot.run_at.replace(tzinfo=self.MSK_TZ)
        return slot.run_at.astimezone(self.MSK_TZ)

    def _latest_due_one_off_slot_key(self, now_msk: datetime) -> Optional[str]:
        due_slots = [
            (slot, self._slot_run_at_msk(slot))
            for slot in self.SLOTS
            if slot.is_one_off and self._slot_run_at_msk(slot) <= now_msk
        ]
        if not due_slots:
            return None
        return max(due_slots, key=lambda item: item[1])[0].key

    async def _is_one_off_slot_processed(self, slot: InteractiveNotificationSlot) -> bool:
        if not slot.is_one_off:
            return False
        if legacy_notify_once_service.is_slot(slot.key):
            return await legacy_notify_once_service.is_terminal()
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(func.count(InteractiveNotificationLog.id)).where(
                    InteractiveNotificationLog.slot_key == slot.key,
                    InteractiveNotificationLog.user_id.is_(None),
                    InteractiveNotificationLog.status.in_(("processed", "skipped", "failed")),
                )
            )
            return int(result.scalar() or 0) > 0

    def _format_slot_for_log(self, slot: InteractiveNotificationSlot) -> str:
        if slot.is_interval:
            seconds = int(slot.interval.total_seconds()) if slot.interval else 0
            return f"{slot.key}=every {seconds}s"
        if slot.is_one_off:
            run_at = self._slot_run_at_msk(slot)
            return f"{slot.key}={run_at.strftime('%d.%m.%Y %H:%M')}"
        return f"{slot.key}={slot.time.strftime('%H:%M')}" if slot.time else slot.key

    async def _process_slot(self, slot: InteractiveNotificationSlot) -> None:
        if legacy_notify_once_service.is_slot(slot.key):
            await legacy_notify_once_service.run(self.bot)
            return

        payload = {
            "timezone": "Europe/Moscow",
            "slot_time": slot.time.strftime("%H:%M") if slot.time else None,
        }
        if slot.interval:
            payload["interval_seconds"] = int(slot.interval.total_seconds())
        if slot.is_one_off:
            payload["scheduled_at_msk"] = self._slot_run_at_msk(slot).isoformat()

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

        if (
            slot.is_interval
            and result.status == "processed"
            and not any((result.payload or {}).get(key) for key in ("sent", "failed", "skipped"))
        ):
            return

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
            slot.time.strftime("%H:%M") if slot.time else "interval",
        )

        if slot.key == cold_solo_offer_service.FIRST_SLOT_KEY:
            return await self._send_cold_solo_first_touch(slot)

        if slot.key == cold_solo_offer_service.SECOND_SLOT_KEY:
            return await self._send_cold_solo_second_touch(slot)

        if slot.key in hot_invoice_offer_service.SLOT_KEYS:
            return await self._send_hot_invoice_touch(slot)

        if slot.key in expired_subscription_offer_service.SLOT_KEYS:
            return await self._send_expired_subscription_touch(slot)

        if slot.key == vpn_deposit_bonus_service.TOUCH_1_SLOT:
            return await self._send_vpn_deposit_bonus_first_touch(slot)

        if slot.key == vpn_deposit_bonus_service.TOUCH_2_SLOT:
            return await self._send_vpn_deposit_bonus_second_touch(slot)

        if legacy_pro_offer_service.get_message_for_slot(slot.key):
            return await self._send_legacy_pro_offer_touch(slot)

        return InteractiveNotificationResult(status="processed")

    async def _send_hot_invoice_touch(
        self,
        slot: InteractiveNotificationSlot,
    ) -> InteractiveNotificationResult:
        if hot_invoice_offer_service.is_debug_enabled():
            return InteractiveNotificationResult(
                status="processed",
                payload={"sent": 0, "failed": 0, "skipped": 0, "debug": True},
            )
        if not self.bot:
            return InteractiveNotificationResult(
                status="skipped",
                error="Bot instance is not available",
            )

        sent = 0
        failed = 0
        skipped = 0
        now = datetime.utcnow()

        async with AsyncSessionLocal() as db:
            after_payment_id = 0
            while True:
                if slot.key == hot_invoice_offer_service.FIRST_SLOT_KEY:
                    candidates = await hot_invoice_offer_service.list_first_touch_candidates(
                        db,
                        now_utc=now,
                        after_payment_id=after_payment_id,
                        limit=self.BATCH_LIMIT,
                    )
                else:
                    candidates = await hot_invoice_offer_service.list_daily_touch_candidates(
                        db,
                        slot.key,
                        now_utc=now,
                        after_payment_id=after_payment_id,
                        limit=self.BATCH_LIMIT,
                    )
                if not candidates:
                    break

                after_payment_id = max(candidate.payment.id for candidate in candidates)
                for candidate in candidates:
                    resolved = await self._resolve_hot_invoice_candidate(
                        db,
                        candidate,
                        slot.key,
                        now,
                    )
                    if resolved is None:
                        skipped += 1
                        continue
                    candidate = resolved

                    if await hot_invoice_offer_service.was_touch_sent(
                        db,
                        user_id=candidate.user.id,
                        slot_key=slot.key,
                        campaign_key=candidate.campaign_key,
                    ):
                        skipped += 1
                        continue
                    if slot.key in (
                        hot_invoice_offer_service.THIRD_SLOT_KEY,
                        hot_invoice_offer_service.FOURTH_MORNING_SLOT_KEY,
                        hot_invoice_offer_service.FOURTH_EVENING_SLOT_KEY,
                    ) and not await hot_invoice_offer_service.was_touch_sent(
                        db,
                        user_id=candidate.user.id,
                        slot_key=hot_invoice_offer_service.SECOND_SLOT_KEY,
                        campaign_key=candidate.campaign_key,
                    ):
                        skipped += 1
                        continue
                    if not await hot_invoice_offer_service.is_campaign_eligible(db, candidate.payment):
                        skipped += 1
                        continue

                    offer = None
                    offer_created = False
                    if slot.key in (
                        hot_invoice_offer_service.FOURTH_MORNING_SLOT_KEY,
                        hot_invoice_offer_service.FOURTH_EVENING_SLOT_KEY,
                    ):
                        offer = await hot_invoice_offer_service.get_available_offer(
                            db, candidate.user.id
                        )
                        if offer is None:
                            if await hot_invoice_offer_service.has_conflicting_active_offer(
                                db, candidate.user.id
                            ):
                                skipped += 1
                                continue
                            offer = await hot_invoice_offer_service.ensure_discount_offer(
                                db, candidate
                            )
                            offer_created = True

                    message_id = await self._send_hot_invoice_message(
                        candidate,
                        slot.key,
                        offer_id=offer.id if offer else None,
                        now_utc=now,
                    )
                    payload = hot_invoice_offer_service.build_campaign_payload(candidate)
                    if offer:
                        payload["offer_id"] = offer.id

                    if message_id:
                        sent += 1
                        await self._record_log(
                            slot,
                            status="sent",
                            user_id=candidate.user.id,
                            telegram_id=candidate.user.telegram_id,
                            message_id=message_id,
                            payload=payload,
                        )
                    else:
                        failed += 1
                        if offer_created and offer is not None:
                            await hot_invoice_offer_service.deactivate_offer(db, offer)
                        await self._record_log(
                            slot,
                            status="failed",
                            user_id=candidate.user.id,
                            telegram_id=candidate.user.telegram_id,
                            error="send_message_failed",
                            payload=payload,
                        )

        return InteractiveNotificationResult(
            status="processed",
            payload={"sent": sent, "failed": failed, "skipped": skipped},
        )

    async def _run_hot_invoice_debug_sequence(self) -> None:
        if not self.bot:
            logger.warning("Segment B debug: bot instance is not available")
            return

        interval = hot_invoice_offer_service.debug_touch_interval()
        logger.warning(
            "Segment B debug is enabled: sending all waves to Artem with %s seconds interval",
            int(interval.total_seconds()),
        )

        for index, slot_key in enumerate(hot_invoice_offer_service.SLOT_KEYS):
            if index > 0:
                await asyncio.sleep(interval.total_seconds())

            async with AsyncSessionLocal() as db:
                candidate = await hot_invoice_offer_service.get_debug_candidate(db)
                if candidate is None:
                    logger.warning(
                        "Segment B debug: Artem user or Platega payment was not found"
                    )
                    return

                offer = None
                if slot_key in (
                    hot_invoice_offer_service.FOURTH_MORNING_SLOT_KEY,
                    hot_invoice_offer_service.FOURTH_EVENING_SLOT_KEY,
                ):
                    offer = await hot_invoice_offer_service.ensure_discount_offer(
                        db,
                        candidate,
                    )

                message_id = await self._send_hot_invoice_message(
                    candidate,
                    slot_key,
                    offer_id=offer.id if offer else None,
                    now_utc=datetime.utcnow(),
                )
                payload = hot_invoice_offer_service.build_campaign_payload(candidate)
                payload["debug"] = True
                if offer:
                    payload["offer_id"] = offer.id

                await self._record_log(
                    InteractiveNotificationSlot(slot_key),
                    status="sent" if message_id else "failed",
                    user_id=candidate.user.id,
                    telegram_id=candidate.user.telegram_id,
                    message_id=message_id,
                    error=None if message_id else "debug_send_failed",
                    payload=payload,
                )

    async def _resolve_hot_invoice_candidate(
        self,
        db,
        candidate: HotInvoiceCandidate,
        slot_key: str,
        now_utc: datetime,
    ) -> Optional[HotInvoiceCandidate]:
        active_payment_id = await hot_invoice_offer_service.get_active_campaign_payment_id(
            db,
            user_id=candidate.user.id,
            now_utc=now_utc,
        )
        if active_payment_id is None:
            return candidate
        if slot_key == hot_invoice_offer_service.FIRST_SLOT_KEY:
            return None
        if active_payment_id == candidate.payment.id:
            return candidate

        payment = await hot_invoice_offer_service.get_payment(db, active_payment_id)
        if payment is None or not hot_invoice_offer_service.is_touch_due(
            payment, slot_key, now_utc
        ):
            return None
        return HotInvoiceCandidate(payment=payment, user=candidate.user)

    async def _send_hot_invoice_message(
        self,
        candidate: HotInvoiceCandidate,
        slot_key: str,
        *,
        offer_id: Optional[int] = None,
        now_utc: Optional[datetime] = None,
    ) -> Optional[int]:
        if not self.bot or candidate.user.telegram_id is None:
            return None

        if slot_key == hot_invoice_offer_service.FIRST_SLOT_KEY:
            minutes_left = hot_invoice_offer_service.invoice_minutes_left(
                candidate.payment,
                now_utc or datetime.utcnow(),
            )
            text = (
                f"<b>⏳ Счёт закроется через {minutes_left} мин</b>\n\n"
                "Почти оформили — оплата не завершилась. Бывает: СБП не открылся или отвлекли.\n\n"
                "Счёт ещё активен, можно закончить в один тап 👇"
            )
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="✅ Завершить оплату", url=candidate.payment.redirect_url)]
                ]
            )
        elif slot_key == hot_invoice_offer_service.SECOND_SLOT_KEY:
            text = (
                "<b>Вчера почти оформили VPN, но что-то помешало 🙂</b>\n\n"
                "Ничего не потерялось — вернуться и оплатить можно в один тап."
            )
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="💎 Вернуться к оплате", callback_data="menu_buy")]
                ]
            )
        elif slot_key == hot_invoice_offer_service.THIRD_SLOT_KEY:
            text = (
                "<b>Мы всё ещё держим для тебя место 👋</b>\n\n"
                "VPN так и ждёт подключения. Если были сложности с оплатой — напиши, поможем."
            )
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="💎 Оформить", callback_data="menu_buy")],
                    [InlineKeyboardButton(text="🆘 Поддержка", callback_data="menu_support")],
                ]
            )
        else:
            if offer_id is None:
                return None
            if slot_key == hot_invoice_offer_service.FOURTH_MORNING_SLOT_KEY:
                text = (
                    "<b>🎁 Держи скидку −20% — только сегодня</b>\n\n"
                    "Раз не сложилось в прошлый раз, вот повод вернуться: скидка на любой тариф.\n\n"
                    "⏰ Сгорит сегодня в 22:00 MSK."
                )
            else:
                text = (
                    "<b>⏰ Час до конца скидки</b>\n\n"
                    "Скидка −20% сгорит в 22:00 MSK. Другого такого предложения не будет."
                )
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Купить Solo -20%",
                            callback_data=f"hot_invoice_offer:claim:{offer_id}:solo",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="Купить Plus -20%",
                            callback_data=f"hot_invoice_offer:claim:{offer_id}:plus",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="Купить Pro -20%",
                            callback_data=f"hot_invoice_offer:claim:{offer_id}:pro",
                        )
                    ],
                ]
            )

        try:
            sent_message = await self.bot.send_message(
                int(candidate.user.telegram_id),
                text,
                reply_markup=keyboard,
                parse_mode="HTML",
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Не удалось отправить Segment B пользователю %s: %s",
                candidate.user.telegram_id,
                exc,
            )
            return None
        return sent_message.message_id

    async def _send_vpn_deposit_bonus_first_touch(
        self,
        slot: InteractiveNotificationSlot,
    ) -> InteractiveNotificationResult:
        if not self.bot:
            return InteractiveNotificationResult(status="skipped", error="Bot instance is not available")

        sent = 0
        failed = 0
        skipped = 0
        now = datetime.utcnow()

        async with AsyncSessionLocal() as db:
            after_log_id = 0
            while True:
                queued_logs = await vpn_deposit_bonus_service.list_touch_1_queue(
                    db,
                    now_utc=now,
                    after_log_id=after_log_id,
                    limit=self.BATCH_LIMIT,
                )
                if not queued_logs:
                    break

                after_log_id = max(log.id for log in queued_logs)
                for log in queued_logs:
                    payload = log.payload if isinstance(log.payload, dict) else {}
                    campaign_key = str(payload.get("campaign_key") or "")
                    if await vpn_deposit_bonus_service.was_paid(db, log.user_id, campaign_key):
                        skipped += 1
                        continue

                    user = await self._get_active_user(db, log.user_id)
                    if not user or not user.telegram_id:
                        skipped += 1
                        continue

                    message_id = await self._send_vpn_deposit_bonus_message(
                        user.telegram_id,
                        first_touch=True,
                    )
                    if message_id:
                        sent += 1
                        log.telegram_id = user.telegram_id
                        await vpn_deposit_bonus_service.mark_touch_sent(
                            db,
                            log,
                            message_id=message_id,
                            action="sent_touch_1",
                        )
                    else:
                        failed += 1
                        log.status = "failed"
                        log.error = "send_message_failed"
                        await db.commit()

        return InteractiveNotificationResult(
            status="processed",
            payload={"sent": sent, "failed": failed, "skipped": skipped},
        )

    async def _send_vpn_deposit_bonus_second_touch(
        self,
        slot: InteractiveNotificationSlot,
    ) -> InteractiveNotificationResult:
        if not self.bot:
            return InteractiveNotificationResult(status="skipped", error="Bot instance is not available")

        sent = 0
        failed = 0
        skipped = 0
        now = datetime.utcnow()

        async with AsyncSessionLocal() as db:
            after_log_id = 0
            while True:
                candidates = await vpn_deposit_bonus_service.list_touch_2_candidates(
                    db,
                    now_utc=now,
                    after_log_id=after_log_id,
                    limit=self.BATCH_LIMIT,
                )
                if not candidates:
                    break

                after_log_id = max(log.id for log in candidates)
                for touch_1_log in candidates:
                    user = await self._get_active_user(db, touch_1_log.user_id)
                    if not user or not user.telegram_id:
                        skipped += 1
                        continue

                    touch_2_log = await vpn_deposit_bonus_service.create_touch_2_log(db, touch_1_log)
                    message_id = await self._send_vpn_deposit_bonus_message(
                        user.telegram_id,
                        first_touch=False,
                    )
                    if message_id:
                        sent += 1
                        touch_2_log.telegram_id = user.telegram_id
                        await vpn_deposit_bonus_service.mark_touch_sent(
                            db,
                            touch_2_log,
                            message_id=message_id,
                            action="sent_touch_2",
                        )
                    else:
                        failed += 1
                        touch_2_log.status = "failed"
                        touch_2_log.error = "send_message_failed"
                        await db.commit()

        return InteractiveNotificationResult(
            status="processed",
            payload={"sent": sent, "failed": failed, "skipped": skipped},
        )

    async def _get_active_user(self, db, user_id: Optional[int]) -> Optional[User]:
        if not user_id:
            return None
        result = await db.execute(
            select(User).where(User.id == user_id, User.status == "active")
        )
        return result.scalar_one_or_none()

    async def _send_vpn_deposit_bonus_message(
        self,
        telegram_id: int,
        *,
        first_touch: bool,
    ) -> Optional[int]:
        if not self.bot:
            return None

        if first_touch:
            text = (
                "🎉 Готово, ты подключён!\n\n"
                "Держи бонус: внеси 10₽ и получи 100₽ на баланс.\n"
                "(любая первая покупка будет на 100₽ дешевле)\n\n"
                "⏰ Предложение активно до 23:59 MSK."
            )
        else:
            text = (
                "🎉 Забирай 100₽ на баланс.\n\n"
                "Напоминаем про бонус: внеси 10₽ и получи 100₽ на баланс.\n\n"
                "⏰ Предложение активно еще 2 часа - до 23:59 MSK."
            )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Внести 10₽ и получить 100₽",
                        callback_data="10rub_vpn_deposit_bonus:pay",
                    )
                ]
            ]
        )

        try:
            sent_message = await self.bot.send_message(
                int(telegram_id),
                text,
                reply_markup=keyboard,
                parse_mode="HTML",
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Не удалось отправить %s пользователю %s: %s",
                vpn_deposit_bonus_service.PURPOSE,
                telegram_id,
                exc,
            )
            return None
        return sent_message.message_id

    async def _send_expired_subscription_touch(
        self,
        slot: InteractiveNotificationSlot,
    ) -> InteractiveNotificationResult:
        if expired_subscription_offer_service.is_debug_enabled():
            return InteractiveNotificationResult(
                status="processed",
                payload={"sent": 0, "failed": 0, "skipped": 0, "debug": True},
            )
        if not self.bot:
            return InteractiveNotificationResult(
                status="skipped",
                error="Bot instance is not available",
            )

        sent = 0
        failed = 0
        skipped = 0
        now = datetime.utcnow()

        async with AsyncSessionLocal() as db:
            after_subscription_id = 0
            while True:
                if slot.key == expired_subscription_offer_service.FIRST_SLOT_KEY:
                    candidates = await expired_subscription_offer_service.list_first_touch_candidates(
                        db,
                        now_utc=now,
                        after_subscription_id=after_subscription_id,
                        limit=self.BATCH_LIMIT,
                    )
                else:
                    candidates = await expired_subscription_offer_service.list_daily_touch_candidates(
                        db,
                        slot.key,
                        now_utc=now,
                        after_subscription_id=after_subscription_id,
                        limit=self.BATCH_LIMIT,
                    )
                if not candidates:
                    break

                after_subscription_id = max(candidate.subscription.id for candidate in candidates)
                for candidate in candidates:
                    if await expired_subscription_offer_service.was_touch_sent(
                        db,
                        user_id=candidate.user.id,
                        slot_key=slot.key,
                        campaign_key=candidate.campaign_key,
                    ):
                        skipped += 1
                        continue
                    if slot.key in (
                        expired_subscription_offer_service.THIRD_SLOT_KEY,
                        expired_subscription_offer_service.FOURTH_MORNING_SLOT_KEY,
                        expired_subscription_offer_service.FOURTH_EVENING_SLOT_KEY,
                    ) and not await expired_subscription_offer_service.was_touch_sent(
                        db,
                        user_id=candidate.user.id,
                        slot_key=expired_subscription_offer_service.SECOND_SLOT_KEY,
                        campaign_key=candidate.campaign_key,
                    ):
                        skipped += 1
                        continue
                    if not await expired_subscription_offer_service.is_campaign_eligible(
                        db,
                        candidate,
                        now_utc=now,
                    ):
                        skipped += 1
                        continue

                    offer = None
                    offer_created = False
                    if slot.key in (
                        expired_subscription_offer_service.FOURTH_MORNING_SLOT_KEY,
                        expired_subscription_offer_service.FOURTH_EVENING_SLOT_KEY,
                    ):
                        offer = await expired_subscription_offer_service.get_available_offer(
                            db, candidate.user.id
                        )
                        if offer is None:
                            if await expired_subscription_offer_service.has_conflicting_active_offer(
                                db, candidate.user.id
                            ):
                                skipped += 1
                                continue
                            offer = await expired_subscription_offer_service.ensure_discount_offer(
                                db, candidate
                            )
                            offer_created = True

                    message_id = await self._send_expired_subscription_message(
                        candidate,
                        slot.key,
                        offer_id=offer.id if offer else None,
                    )
                    payload = expired_subscription_offer_service.build_campaign_payload(candidate)
                    if offer:
                        payload["offer_id"] = offer.id

                    if message_id:
                        sent += 1
                        await self._record_log(
                            slot,
                            status="sent",
                            user_id=candidate.user.id,
                            telegram_id=candidate.user.telegram_id,
                            message_id=message_id,
                            payload=payload,
                        )
                    else:
                        failed += 1
                        if offer_created and offer is not None:
                            await expired_subscription_offer_service.deactivate_offer(db, offer)
                        await self._record_log(
                            slot,
                            status="failed",
                            user_id=candidate.user.id,
                            telegram_id=candidate.user.telegram_id,
                            error="send_message_failed",
                            payload=payload,
                        )

        return InteractiveNotificationResult(
            status="processed",
            payload={"sent": sent, "failed": failed, "skipped": skipped},
        )

    async def _run_expired_subscription_debug_sequence(self) -> None:
        if not self.bot:
            logger.warning("Segment C debug: bot instance is not available")
            return

        interval = expired_subscription_offer_service.debug_touch_interval()
        logger.warning(
            "Segment C debug is enabled: sending all waves to Artem with %s seconds interval",
            int(interval.total_seconds()),
        )

        for index, slot_key in enumerate(expired_subscription_offer_service.SLOT_KEYS):
            if index > 0:
                await asyncio.sleep(interval.total_seconds())

            async with AsyncSessionLocal() as db:
                candidate = await expired_subscription_offer_service.get_debug_candidate(db)
                if candidate is None:
                    logger.warning(
                        "Segment C debug: Artem user or tiered subscription was not found"
                    )
                    return

                offer = None
                if slot_key in (
                    expired_subscription_offer_service.FOURTH_MORNING_SLOT_KEY,
                    expired_subscription_offer_service.FOURTH_EVENING_SLOT_KEY,
                ):
                    offer = await expired_subscription_offer_service.ensure_discount_offer(
                        db,
                        candidate,
                    )

                message_id = await self._send_expired_subscription_message(
                    candidate,
                    slot_key,
                    offer_id=offer.id if offer else None,
                )
                payload = expired_subscription_offer_service.build_campaign_payload(candidate)
                payload["debug"] = True
                if offer:
                    payload["offer_id"] = offer.id

                await self._record_log(
                    InteractiveNotificationSlot(slot_key),
                    status="sent" if message_id else "failed",
                    user_id=candidate.user.id,
                    telegram_id=candidate.user.telegram_id,
                    message_id=message_id,
                    error=None if message_id else "debug_send_failed",
                    payload=payload,
                )

    async def _send_expired_subscription_message(
        self,
        candidate: ExpiredSubscriptionCandidate,
        slot_key: str,
        *,
        offer_id: Optional[int] = None,
    ) -> Optional[int]:
        if not self.bot or candidate.user.telegram_id is None:
            return None

        plan_code = candidate.plan_code
        plan_name = candidate.plan_name
        if slot_key == expired_subscription_offer_service.FIRST_SLOT_KEY:
            text = (
                "<b>⛔ Подписка закончилась</b>\n\n"
                "Доступ к серверам отключён. Продли, чтобы вернуть VPN — займёт минуту."
            )
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="💎 Восстановить подписку",
                            callback_data="subscription_renew_current",
                        )
                    ]
                ]
            )
        elif slot_key == expired_subscription_offer_service.SECOND_SLOT_KEY:
            text = (
                "<b>Без VPN уже сутки — всё ещё нужен? 🙂</b>\n\n"
                "Твой прежний тариф на месте, восстановить можно в один тап."
            )
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="💎 Вернуть доступ",
                            callback_data="subscription_renew_current",
                        )
                    ]
                ]
            )
        elif slot_key == expired_subscription_offer_service.THIRD_SLOT_KEY:
            text = (
                "<b>Возвращайся — мы соскучились 👋</b>\n\n"
                "VPN готов подключиться там, где остановились. Настройки сохранены."
            )
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="💎 Восстановить",
                            callback_data="subscription_renew_current",
                        )
                    ],
                    [InlineKeyboardButton(text="🆘 Поддержка", callback_data="menu_support")],
                ]
            )
        else:
            if not plan_code or offer_id is None:
                return None
            if slot_key == expired_subscription_offer_service.FOURTH_MORNING_SLOT_KEY:
                text = (
                    "<b>🎁 Скидка −20% на возврат — только сегодня</b>\n\n"
                    "Мы бы хотели, чтобы ты остался. Держи скидку на восстановление тарифа.\n\n"
                    "⏰ До 22:00 MSK."
                )
            else:
                text = (
                    "<b>⏰ Час до конца скидки</b>\n\n"
                    "Скидка −20% сгорит в 22:00 MSK. Другого такого предложения не будет."
                )
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=f"💎 Восстановить {plan_name} −20%",
                            callback_data=f"expired_subscription_offer:claim:{offer_id}:{plan_code}",
                        )
                    ]
                ]
            )

        try:
            sent_message = await self.bot.send_message(
                int(candidate.user.telegram_id),
                text,
                reply_markup=keyboard,
                parse_mode="HTML",
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Не удалось отправить Segment C пользователю %s: %s",
                candidate.user.telegram_id,
                exc,
            )
            return None
        return sent_message.message_id

    async def _send_legacy_pro_offer_touch(
        self,
        slot: InteractiveNotificationSlot,
    ) -> InteractiveNotificationResult:
        if not self.bot:
            return InteractiveNotificationResult(
                status="skipped",
                error="Bot instance is not available",
            )

        message = legacy_pro_offer_service.get_message_for_slot(slot.key)
        if message is None:
            return InteractiveNotificationResult(status="skipped", error="Unknown Legacy Pro slot")

        if legacy_pro_offer_service.is_debug_enabled():
            sent = 0
            failed = 0
            skipped = 0

            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(User)
                    .options(selectinload(User.subscription))
                    .where(User.id == legacy_pro_offer_service.debug_user_id())
                    .limit(1)
                )
                user = result.scalars().first()
                if user is None:
                    return InteractiveNotificationResult(
                        status="processed",
                        payload={
                            "sent": sent,
                            "failed": failed,
                            "skipped": skipped + 1,
                            "debug_user_id": legacy_pro_offer_service.debug_user_id(),
                            "debug_reason": "user_not_found",
                        },
                    )

                member = legacy_pro_offer_service.build_debug_member(user, message.groups)
                if member is None:
                    return InteractiveNotificationResult(
                        status="processed",
                        user_id=user.id,
                        telegram_id=user.telegram_id,
                        payload={
                            "sent": sent,
                            "failed": failed,
                            "skipped": skipped + 1,
                            "debug_user_id": legacy_pro_offer_service.debug_user_id(),
                            "debug_reason": "telegram_id_missing",
                        },
                    )

                if await legacy_pro_offer_service.was_slot_sent(db, user.id, slot.key):
                    return InteractiveNotificationResult(
                        status="processed",
                        user_id=user.id,
                        telegram_id=user.telegram_id,
                        payload={
                            "sent": sent,
                            "failed": failed,
                            "skipped": skipped + 1,
                            "debug_user_id": legacy_pro_offer_service.debug_user_id(),
                            "debug_reason": "slot_already_sent",
                        },
                    )

                offer = await legacy_pro_offer_service.ensure_offer(db, user, member)
                message_id = await self._send_legacy_pro_offer_message(
                    member.telegram_id,
                    text=message.text,
                    button_text=message.button_text,
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
                        payload={
                            "offer_id": offer.id,
                            "csv_group": member.group,
                            "price_kopeks": member.price_kopeks,
                            "debug_user_id": legacy_pro_offer_service.debug_user_id(),
                        },
                    )
                else:
                    failed += 1
                    await self._record_log(
                        slot,
                        status="failed",
                        user_id=user.id,
                        telegram_id=user.telegram_id,
                        error="send_message_failed",
                        payload={
                            "offer_id": offer.id,
                            "csv_group": member.group,
                            "price_kopeks": member.price_kopeks,
                            "debug_user_id": legacy_pro_offer_service.debug_user_id(),
                        },
                    )

            return InteractiveNotificationResult(
                status="processed",
                payload={
                    "sent": sent,
                    "failed": failed,
                    "skipped": skipped,
                    "debug_user_id": legacy_pro_offer_service.debug_user_id(),
                },
            )

        members = legacy_pro_offer_service.list_members(message.groups)
        member_by_tg_id = {member.telegram_id: member for member in members}
        telegram_ids = list(member_by_tg_id.keys())
        sent = 0
        failed = 0
        skipped = 0

        async with AsyncSessionLocal() as db:
            batch_size = self.BATCH_LIMIT
            for offset in range(0, len(telegram_ids), batch_size):
                batch_ids = telegram_ids[offset:offset + batch_size]
                result = await db.execute(
                    select(User)
                    .options(selectinload(User.subscription))
                    .where(User.telegram_id.in_(batch_ids))
                    .order_by(User.id.asc())
                )
                users = list(result.scalars().all())

                found_ids = {int(user.telegram_id) for user in users if user.telegram_id is not None}
                skipped += len(set(batch_ids) - found_ids)

                for user in users:
                    member = member_by_tg_id.get(int(user.telegram_id))
                    if member is None:
                        skipped += 1
                        continue
                    if not legacy_pro_offer_service.is_eligible_user(user, member):
                        skipped += 1
                        continue
                    if await legacy_pro_offer_service.was_slot_sent(db, user.id, slot.key):
                        skipped += 1
                        continue

                    offer = await legacy_pro_offer_service.ensure_offer(db, user, member)
                    message_id = await self._send_legacy_pro_offer_message(
                        int(user.telegram_id),
                        text=message.text,
                        button_text=message.button_text,
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
                            payload={
                                "offer_id": offer.id,
                                "csv_group": member.group,
                                "price_kopeks": member.price_kopeks,
                            },
                        )
                    else:
                        failed += 1
                        await self._record_log(
                            slot,
                            status="failed",
                            user_id=user.id,
                            telegram_id=user.telegram_id,
                            error="send_message_failed",
                            payload={
                                "offer_id": offer.id,
                                "csv_group": member.group,
                                "price_kopeks": member.price_kopeks,
                            },
                        )

        return InteractiveNotificationResult(
            status="processed",
            payload={"sent": sent, "failed": failed, "skipped": skipped},
        )

    async def _send_legacy_pro_offer_message(
        self,
        telegram_id: int,
        *,
        text: str,
        button_text: str,
        offer_id: int,
    ) -> Optional[int]:
        if not self.bot:
            return None

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=button_text,
                        callback_data=f"legacy_pro_offer:claim:{offer_id}",
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
                "Не удалось отправить Legacy Pro offer пользователю %s: %s",
                telegram_id,
                exc,
            )
            return None

        return sent_message.message_id

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
                    first_touch_result = await db.execute(
                        select(InteractiveNotificationLog.created_at)
                        .where(
                            InteractiveNotificationLog.user_id == user.id,
                            InteractiveNotificationLog.slot_key == cold_solo_offer_service.FIRST_SLOT_KEY,
                            InteractiveNotificationLog.status == "sent",
                            InteractiveNotificationLog.created_at >= day_start,
                            InteractiveNotificationLog.created_at < day_end,
                        )
                        .order_by(InteractiveNotificationLog.created_at.desc())
                        .limit(1)
                    )
                    first_touch_at = first_touch_result.scalar_one_or_none()
                    if first_touch_at and await hot_invoice_offer_service.has_unpaid_invoice_since(
                        db,
                        user_id=user.id,
                        since=first_touch_at,
                    ):
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
