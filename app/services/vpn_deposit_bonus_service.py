from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time as datetime_time, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.promo_offer_log import log_promo_offer_action
from app.database.models import InteractiveNotificationLog, PromoOfferLog, User


logger = logging.getLogger(__name__)

IS_DEBUG = False
DEBUG_USER_ID = 18835


@dataclass(frozen=True)
class VpnDepositBonusPaymentDecision:
    is_campaign_payment: bool
    is_duplicate: bool = False
    credit_amount_kopeks: int = 0
    referral_amount_kopeks: int = 0
    campaign_key: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None


class VpnDepositBonusService:
    PURPOSE = "10rub_vpn_deposit_bonus"
    SOURCE = PURPOSE
    EFFECT_TYPE = "deposit_bonus"
    TOUCH_1_SLOT = "10rub_vpn_deposit_bonus_1"
    TOUCH_2_SLOT = "10rub_vpn_deposit_bonus_2"

    INVOICE_AMOUNT_KOPEKS = 1_000
    BONUS_AMOUNT_KOPEKS = 10_000
    MSK_TZ = ZoneInfo("Europe/Moscow")

    def is_debug_enabled(self) -> bool:
        return bool(IS_DEBUG)

    def is_debug_user_id_allowed(self, user_id: Optional[int]) -> bool:
        if not self.is_debug_enabled():
            return True
        return bool(user_id and int(user_id) == int(DEBUG_USER_ID))

    def is_debug_user_allowed(self, user: User | None) -> bool:
        if not self.is_debug_enabled():
            return True
        if user is None:
            return False
        return bool(
            getattr(user, "id", None) == DEBUG_USER_ID
            or getattr(user, "telegram_id", None) == DEBUG_USER_ID
        )

    def campaign_key(self, user_id: int) -> str:
        return f"{self.PURPOSE}:{user_id}"

    def build_payment_metadata(self, user_id: int) -> dict[str, Any]:
        return {
            "purpose": self.PURPOSE,
            "campaign_key": self.campaign_key(user_id),
            "invoice_amount_kopeks": self.INVOICE_AMOUNT_KOPEKS,
            "bonus_amount_kopeks": self.BONUS_AMOUNT_KOPEKS,
        }

    def is_campaign_metadata(self, metadata: Any) -> bool:
        if not isinstance(metadata, dict):
            return False
        return metadata.get("purpose") == self.PURPOSE

    def extract_payment_metadata(self, payment_or_metadata: Any) -> Optional[dict[str, Any]]:
        if isinstance(payment_or_metadata, dict):
            metadata = payment_or_metadata
        else:
            metadata = getattr(payment_or_metadata, "metadata_json", None) or {}

        if self.is_campaign_metadata(metadata):
            return dict(metadata)
        return None

    def is_campaign_stars_payload(self, payload: str | None) -> bool:
        return bool(payload and payload.startswith(f"{self.PURPOSE}_"))

    def build_stars_payload(self, user_id: int, amount_kopeks: int) -> str:
        return f"{self.PURPOSE}_{user_id}_{amount_kopeks}"

    def metadata_from_stars_payload(self, user_id: int, payload: str | None) -> Optional[dict[str, Any]]:
        if not self.is_campaign_stars_payload(payload):
            return None
        return self.build_payment_metadata(user_id)

    def expires_at_for_detection(self, detected_at_utc: datetime) -> datetime:
        detected_msk = self._to_msk(detected_at_utc)
        expires_msk = datetime.combine(
            detected_msk.date(),
            datetime_time(hour=23, minute=59, second=59),
            tzinfo=self.MSK_TZ,
        )
        return expires_msk.astimezone(timezone.utc).replace(tzinfo=None)

    def should_skip_reminder(self, detected_at_utc: datetime) -> bool:
        detected_msk = self._to_msk(detected_at_utc)
        return detected_msk.time() >= datetime_time(hour=21)

    async def on_first_vpn_connection_detected(
        self,
        db: AsyncSession,
        user: User,
        source: str,
        panel_first_connected_at: Optional[datetime | str] = None,
    ) -> bool:
        if not user or not user.id or not user.telegram_id:
            return False
        if not self.is_debug_user_allowed(user):
            logger.info(
                "Skipped %s for user %s: debug mode allows only user %s",
                self.PURPOSE,
                user.telegram_id,
                DEBUG_USER_ID,
            )
            return False

        campaign_key = self.campaign_key(user.id)
        if await self._has_touch_log(db, user.id, self.TOUCH_1_SLOT, campaign_key):
            return False

        detected_at = datetime.utcnow()
        expires_at = self.expires_at_for_detection(detected_at)
        skip_reminder = self.should_skip_reminder(detected_at)
        payload = {
            "campaign_key": campaign_key,
            "detected_at": detected_at.isoformat(),
            "panel_first_connected_at": self._serialize_datetime(panel_first_connected_at),
            "source": source,
            "expires_at": expires_at.isoformat(),
            "skip_reminder": skip_reminder,
            "invoice_amount_kopeks": self.INVOICE_AMOUNT_KOPEKS,
            "bonus_amount_kopeks": self.BONUS_AMOUNT_KOPEKS,
        }

        db.add(
            InteractiveNotificationLog(
                slot_key=self.TOUCH_1_SLOT,
                user_id=user.id,
                telegram_id=user.telegram_id,
                status="queued",
                payload=payload,
            )
        )
        await db.commit()

        await self._log_once(
            db,
            user_id=user.id,
            action="detected",
            campaign_key=campaign_key,
            details=payload,
        )
        logger.info("Queued %s for user %s from %s", self.PURPOSE, user.telegram_id, source)
        return True

    async def list_touch_1_queue(
        self,
        db: AsyncSession,
        *,
        now_utc: datetime,
        after_log_id: int = 0,
        limit: int = 500,
    ) -> list[InteractiveNotificationLog]:
        now = self._to_utc_naive(now_utc)
        logs: list[InteractiveNotificationLog] = []
        cursor = after_log_id
        while len(logs) < limit:
            result = await db.execute(
                select(InteractiveNotificationLog)
                .where(
                    InteractiveNotificationLog.id > cursor,
                    InteractiveNotificationLog.slot_key == self.TOUCH_1_SLOT,
                    InteractiveNotificationLog.status == "queued",
                )
                .order_by(InteractiveNotificationLog.id.asc())
                .limit(limit)
            )
            batch = result.scalars().all()
            if not batch:
                break

            cursor = max(log.id for log in batch)
            for log in batch:
                if not self.is_debug_user_id_allowed(getattr(log, "user_id", None)):
                    continue
                if self._is_payload_expired(log.payload, now):
                    log.status = "expired"
                    continue
                logs.append(log)
                if len(logs) >= limit:
                    break
            if len(batch) < limit:
                break

        await db.commit()
        return logs

    async def list_touch_2_candidates(
        self,
        db: AsyncSession,
        *,
        now_utc: datetime,
        after_log_id: int = 0,
        limit: int = 500,
    ) -> list[InteractiveNotificationLog]:
        today_msk = self._to_msk(now_utc).date()
        now = self._to_utc_naive(now_utc)
        candidates: list[InteractiveNotificationLog] = []
        cursor = after_log_id
        while len(candidates) < limit:
            result = await db.execute(
                select(InteractiveNotificationLog)
                .where(
                    InteractiveNotificationLog.id > cursor,
                    InteractiveNotificationLog.slot_key == self.TOUCH_1_SLOT,
                    InteractiveNotificationLog.status == "sent",
                )
                .order_by(InteractiveNotificationLog.id.asc())
                .limit(limit)
            )
            logs = result.scalars().all()
            if not logs:
                break

            cursor = max(log.id for log in logs)
            for log in logs:
                if not self.is_debug_user_id_allowed(getattr(log, "user_id", None)):
                    continue
                payload = log.payload if isinstance(log.payload, dict) else {}
                if payload.get("skip_reminder"):
                    continue
                if self._is_payload_expired(payload, now):
                    continue
                campaign_key = payload.get("campaign_key")
                if not campaign_key or await self.was_paid(db, log.user_id, str(campaign_key)):
                    continue
                if await self._has_touch_log(db, log.user_id, self.TOUCH_2_SLOT, str(campaign_key), statuses=("sent", "queued")):
                    continue
                detected_at = self._parse_datetime(payload.get("detected_at")) or log.created_at
                if self._to_msk(detected_at).date() != today_msk:
                    continue
                candidates.append(log)
                if len(candidates) >= limit:
                    break
            if len(logs) < limit:
                break
        return candidates

    async def mark_touch_sent(
        self,
        db: AsyncSession,
        log: InteractiveNotificationLog,
        *,
        message_id: int,
        action: str,
    ) -> None:
        payload = dict(log.payload or {})
        payload["sent_at"] = datetime.utcnow().isoformat()
        log.status = "sent"
        log.message_id = message_id
        log.payload = payload
        await db.commit()
        await self._log_once(
            db,
            user_id=log.user_id,
            action=action,
            campaign_key=str(payload.get("campaign_key")),
            details={**payload, "message_id": message_id},
        )

    async def create_touch_2_log(
        self,
        db: AsyncSession,
        touch_1_log: InteractiveNotificationLog,
    ) -> InteractiveNotificationLog:
        payload = dict(touch_1_log.payload or {})
        log = InteractiveNotificationLog(
            slot_key=self.TOUCH_2_SLOT,
            user_id=touch_1_log.user_id,
            telegram_id=touch_1_log.telegram_id,
            status="queued",
            payload=payload,
        )
        db.add(log)
        await db.commit()
        await db.refresh(log)
        return log

    async def was_paid(self, db: AsyncSession, user_id: Optional[int], campaign_key: str) -> bool:
        if not user_id or not campaign_key:
            return False
        result = await db.execute(
            select(PromoOfferLog.details).where(
                PromoOfferLog.user_id == user_id,
                PromoOfferLog.source == self.SOURCE,
                PromoOfferLog.action == "paid",
            )
        )
        return any(
            isinstance(details, dict) and details.get("campaign_key") == campaign_key
            for details in result.scalars().all()
        )

    async def resolve_payment_decision(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        payment_amount_kopeks: int,
        metadata: Optional[dict[str, Any]],
    ) -> VpnDepositBonusPaymentDecision:
        if not self.is_campaign_metadata(metadata):
            return VpnDepositBonusPaymentDecision(is_campaign_payment=False)
        if not self.is_debug_user_id_allowed(user_id):
            return VpnDepositBonusPaymentDecision(is_campaign_payment=False)
        campaign_key = str(metadata.get("campaign_key") or self.campaign_key(user_id))
        is_duplicate = await self.was_paid(db, user_id, campaign_key)
        credit_amount = payment_amount_kopeks if is_duplicate else self.BONUS_AMOUNT_KOPEKS
        return VpnDepositBonusPaymentDecision(
            is_campaign_payment=True,
            is_duplicate=is_duplicate,
            credit_amount_kopeks=credit_amount,
            referral_amount_kopeks=payment_amount_kopeks,
            campaign_key=campaign_key,
            metadata=metadata,
        )

    async def record_invoice_created(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        metadata: dict[str, Any],
        provider: str,
        payment_id: Any = None,
    ) -> None:
        await self._log_once(
            db,
            user_id=user_id,
            action="invoice_created",
            campaign_key=str(metadata.get("campaign_key")),
            details={**metadata, "provider": provider, "payment_id": payment_id},
        )

    async def get_payable_campaign_metadata(
        self,
        db: AsyncSession,
        user_id: int,
        *,
        now_utc: Optional[datetime] = None,
    ) -> Optional[dict[str, Any]]:
        result = await db.execute(
            select(InteractiveNotificationLog.payload)
            .where(
                InteractiveNotificationLog.user_id == user_id,
                InteractiveNotificationLog.slot_key == self.TOUCH_1_SLOT,
                InteractiveNotificationLog.status.in_(("queued", "sent")),
            )
            .order_by(InteractiveNotificationLog.created_at.desc())
        )
        if not self.is_debug_user_id_allowed(user_id):
            return None
        now = now_utc or datetime.utcnow()
        for payload in result.scalars().all():
            if not isinstance(payload, dict):
                continue
            campaign_key = str(payload.get("campaign_key") or "")
            if not campaign_key or self._is_payload_expired(payload, now):
                continue
            if await self.was_paid(db, user_id, campaign_key):
                continue
            return {
                **self.build_payment_metadata(user_id),
                "campaign_key": campaign_key,
                "expires_at": payload.get("expires_at"),
            }
        return None

    async def record_payment_processed(
        self,
        db: AsyncSession,
        *,
        user: User,
        transaction: Any,
        decision: VpnDepositBonusPaymentDecision,
        provider: str,
    ) -> None:
        if not decision.is_campaign_payment or not decision.campaign_key:
            return
        action = "duplicate_payment" if decision.is_duplicate else "paid"
        await self._log_once(
            db,
            user_id=user.id,
            action=action,
            campaign_key=decision.campaign_key,
            details={
                **(decision.metadata or {}),
                "provider": provider,
                "transaction_id": getattr(transaction, "id", None),
                "credit_amount_kopeks": decision.credit_amount_kopeks,
                "duplicate": decision.is_duplicate,
            },
        )

    async def send_success_message(self, bot: Any, user: User) -> None:
        if not bot or not user.telegram_id:
            return
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Выбрать Solo", callback_data="tariff_select:solo")],
                [InlineKeyboardButton(text="Выбрать Plus", callback_data="tariff_select:plus")],
                [InlineKeyboardButton(text="Выбрать Pro", callback_data="tariff_select:pro")],
            ]
        )
        await bot.send_message(
            user.telegram_id,
            "✅ Готово! На балансе 100₽\n\n"
            "При первой покупке сумма покупки будет уменьшена на 100₽.",
            reply_markup=keyboard,
            parse_mode="HTML",
        )

    async def _has_touch_log(
        self,
        db: AsyncSession,
        user_id: Optional[int],
        slot_key: str,
        campaign_key: str,
        *,
        statuses: tuple[str, ...] = ("queued", "sent"),
    ) -> bool:
        if not user_id:
            return False
        result = await db.execute(
            select(InteractiveNotificationLog.payload).where(
                InteractiveNotificationLog.user_id == user_id,
                InteractiveNotificationLog.slot_key == slot_key,
                InteractiveNotificationLog.status.in_(statuses),
            )
        )
        return any(
            isinstance(payload, dict) and payload.get("campaign_key") == campaign_key
            for payload in result.scalars().all()
        )

    async def _log_once(
        self,
        db: AsyncSession,
        *,
        user_id: Optional[int],
        action: str,
        campaign_key: str,
        details: dict[str, Any],
    ) -> None:
        if user_id and campaign_key:
            result = await db.execute(
                select(PromoOfferLog.details).where(
                    PromoOfferLog.user_id == user_id,
                    PromoOfferLog.source == self.SOURCE,
                    PromoOfferLog.action == action,
                )
            )
            if any(
                isinstance(existing, dict) and existing.get("campaign_key") == campaign_key
                for existing in result.scalars().all()
            ):
                return

        await log_promo_offer_action(
            db,
            user_id=user_id,
            offer_id=None,
            action=action,
            source=self.SOURCE,
            effect_type=self.EFFECT_TYPE,
            details=details,
        )

    def _is_payload_expired(self, payload: Any, now_utc: datetime) -> bool:
        if not isinstance(payload, dict):
            return True
        expires_at = self._parse_datetime(payload.get("expires_at"))
        if not expires_at:
            return True
        return self._to_utc_naive(expires_at) <= self._to_utc_naive(now_utc)

    def _parse_datetime(self, value: Any) -> Optional[datetime]:
        if isinstance(value, datetime):
            return value
        if isinstance(value, str) and value:
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
        return None

    def _serialize_datetime(self, value: datetime | str | None) -> Optional[str]:
        parsed = self._parse_datetime(value)
        if parsed is None:
            return str(value) if value else None
        return self._to_utc_naive(parsed).isoformat()

    def _to_msk(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(self.MSK_TZ)

    @staticmethod
    def _to_utc_naive(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value
        return value.astimezone(timezone.utc).replace(tzinfo=None)


vpn_deposit_bonus_service = VpnDepositBonusService()
