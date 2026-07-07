from __future__ import annotations

import logging
from datetime import datetime, time as datetime_time, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.crud.discount_offer import mark_offer_claimed, upsert_discount_offer
from app.database.models import (
    CloudPaymentsPayment,
    CryptoBotPayment,
    DiscountOffer,
    HeleketPayment,
    InteractiveNotificationLog,
    MulenPayPayment,
    Pal24Payment,
    PlategaPayment,
    Subscription,
    Transaction,
    TransactionType,
    User,
    UserStatus,
    WataPayment,
    YooKassaPayment,
)


logger = logging.getLogger(__name__)


IS_ARTEM_DEBUG = False
ARTEM_DEBUG_USER_ID = 18835


class ColdSoloOfferService:
    """Fixed-price post-trial offer: Solo 1 year for 990 RUB until 22:00 MSK."""

    NOTIFICATION_TYPE = "cold_solo_990"
    EFFECT_TYPE = "fixed_price_tariff"
    PLAN_CODE = "solo"
    PERIOD_DAYS = 360
    PRICE_KOPEKS = 99_000
    ORIGINAL_PRICE_KOPEKS = 156_000
    MSK_TZ = ZoneInfo("Europe/Moscow")

    FIRST_SLOT_KEY = "cold_solo_990_a_1"
    SECOND_SLOT_KEY = "cold_solo_990_a_2"

    _PAYMENT_MODELS = (
        YooKassaPayment,
        CryptoBotPayment,
        HeleketPayment,
        MulenPayPayment,
        Pal24Payment,
        WataPayment,
        PlategaPayment,
        CloudPaymentsPayment,
    )

    def is_allowed_user(self, user_id: int) -> bool:
        if IS_ARTEM_DEBUG:
            return int(user_id) == ARTEM_DEBUG_USER_ID
        return True

    def offer_expires_at(self, now_utc: Optional[datetime] = None) -> datetime:
        now_msk = self._to_msk(now_utc or datetime.utcnow())
        expires_msk = datetime.combine(
            now_msk.date(),
            datetime_time(hour=22, minute=0),
            tzinfo=self.MSK_TZ,
        )
        return expires_msk.astimezone(timezone.utc).replace(tzinfo=None)

    def is_expired_today(self, now_utc: Optional[datetime] = None) -> bool:
        return datetime.utcnow() >= self.offer_expires_at(now_utc)

    async def has_any_invoice(self, db: AsyncSession, user_id: int) -> bool:
        for model in self._PAYMENT_MODELS:
            result = await db.execute(
                select(func.count(model.id)).where(model.user_id == user_id)
            )
            if int(result.scalar() or 0) > 0:
                return True
        return False

    async def has_paid(self, db: AsyncSession, user: User) -> bool:
        if getattr(user, "has_had_paid_subscription", False) or getattr(user, "has_made_first_topup", False):
            return True
        result = await db.execute(
            select(func.count(Transaction.id)).where(
                Transaction.user_id == user.id,
                Transaction.is_completed.is_(True),
                Transaction.type.in_(
                    (
                        TransactionType.DEPOSIT.value,
                        TransactionType.SUBSCRIPTION_PAYMENT.value,
                    )
                ),
            )
        )
        return int(result.scalar() or 0) > 0

    async def has_successful_subscription_purchase(self, db: AsyncSession, user: User) -> bool:
        if getattr(user, "has_had_paid_subscription", False):
            return True
        result = await db.execute(
            select(func.count(Transaction.id)).where(
                Transaction.user_id == user.id,
                Transaction.is_completed.is_(True),
                Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
            )
        )
        return int(result.scalar() or 0) > 0

    async def is_initially_eligible(
        self,
        db: AsyncSession,
        user: User,
        *,
        now_utc: Optional[datetime] = None,
    ) -> bool:
        if not user or not getattr(user, "telegram_id", None):
            return False
        if not self.is_allowed_user(user.id):
            return False
        if getattr(user, "status", None) != UserStatus.ACTIVE.value:
            return False

        subscription = getattr(user, "subscription", None)
        if not subscription or not getattr(subscription, "is_trial", False):
            return False

        now = now_utc or datetime.utcnow()
        end_date = getattr(subscription, "end_date", None)
        if not end_date or end_date > now - timedelta(hours=10):
            return False

        if await self.has_paid(db, user):
            return False
        if await self.has_any_invoice(db, user.id):
            return False

        existing = await self.get_any_offer(db, user.id)
        return existing is None

    async def get_any_offer(self, db: AsyncSession, user_id: int) -> Optional[DiscountOffer]:
        result = await db.execute(
            select(DiscountOffer)
            .where(
                DiscountOffer.user_id == user_id,
                DiscountOffer.notification_type == self.NOTIFICATION_TYPE,
            )
            .order_by(DiscountOffer.created_at.desc())
        )
        return result.scalars().first()

    async def get_active_offer(self, db: AsyncSession, user_id: int) -> Optional[DiscountOffer]:
        if not self.is_allowed_user(user_id):
            return None
        now = datetime.utcnow()
        result = await db.execute(
            select(DiscountOffer)
            .options(selectinload(DiscountOffer.user), selectinload(DiscountOffer.subscription))
            .where(
                DiscountOffer.user_id == user_id,
                DiscountOffer.notification_type == self.NOTIFICATION_TYPE,
                DiscountOffer.effect_type == self.EFFECT_TYPE,
                DiscountOffer.is_active.is_(True),
                DiscountOffer.claimed_at.is_(None),
                DiscountOffer.expires_at > now,
            )
            .order_by(DiscountOffer.expires_at.asc())
        )
        return result.scalars().first()

    async def ensure_offer(
        self,
        db: AsyncSession,
        user: User,
        *,
        now_utc: Optional[datetime] = None,
    ) -> DiscountOffer:
        if not self.is_allowed_user(user.id):
            raise ValueError("cold Solo offer is disabled for this user")

        existing = await self.get_active_offer(db, user.id)
        if existing:
            return existing

        expires_at = self.offer_expires_at(now_utc)
        now = now_utc or datetime.utcnow()
        valid_hours = max(1, int((expires_at - now).total_seconds() // 3600))
        extra_data = self.build_extra_data()
        extra_data["expires_at_msk"] = self._to_msk(expires_at).isoformat()

        offer = await upsert_discount_offer(
            db,
            user_id=user.id,
            subscription_id=getattr(getattr(user, "subscription", None), "id", None),
            notification_type=self.NOTIFICATION_TYPE,
            discount_percent=0,
            bonus_amount_kopeks=0,
            valid_hours=valid_hours,
            effect_type=self.EFFECT_TYPE,
            extra_data=extra_data,
        )
        offer.expires_at = expires_at
        await db.commit()
        await db.refresh(offer)
        return offer

    def build_extra_data(self) -> dict[str, Any]:
        return {
            "offer_type": "cold_solo_990",
            "plan_code": self.PLAN_CODE,
            "period_days": self.PERIOD_DAYS,
            "price_kopeks": self.PRICE_KOPEKS,
            "original_price_kopeks": self.ORIGINAL_PRICE_KOPEKS,
            "title": "1 год Solo за 990₽",
            "button_text": "Забрать Solo за 990₽",
            "message_text": (
                "Solo на 1 год за 990₽ вместо 1560₽. "
                "Предложение действует до 22:00 MSK."
            ),
        }

    def validate_offer_for_plan(
        self,
        offer: Optional[DiscountOffer],
        *,
        plan_code: Optional[str],
        period_days: Optional[int],
    ) -> bool:
        if not offer:
            return False
        if getattr(offer, "notification_type", None) != self.NOTIFICATION_TYPE:
            return False
        if getattr(offer, "effect_type", None) != self.EFFECT_TYPE:
            return False
        if not getattr(offer, "is_active", False) or getattr(offer, "claimed_at", None):
            return False
        expires_at = getattr(offer, "expires_at", None)
        if not expires_at or expires_at <= datetime.utcnow():
            return False

        extra = getattr(offer, "extra_data", None) or {}
        return (
            str(extra.get("plan_code") or self.PLAN_CODE).lower() == (plan_code or "").lower()
            and int(extra.get("period_days") or self.PERIOD_DAYS) == int(period_days or 0)
        )

    async def get_price_override(
        self,
        db: AsyncSession,
        user_id: int,
        *,
        plan_code: Optional[str],
        period_days: Optional[int],
    ) -> tuple[Optional[int], Optional[DiscountOffer]]:
        offer = await self.get_active_offer(db, user_id)
        user = getattr(offer, "user", None) if offer else None
        if user is not None and await self.has_successful_subscription_purchase(db, user):
            return None, None
        if not self.validate_offer_for_plan(
            offer,
            plan_code=plan_code,
            period_days=period_days,
        ):
            return None, None

        extra = getattr(offer, "extra_data", None) or {}
        try:
            price = int(extra.get("price_kopeks") or self.PRICE_KOPEKS)
        except (TypeError, ValueError):
            price = self.PRICE_KOPEKS
        return price, offer

    async def mark_claimed_after_purchase(self, db: AsyncSession, offer: Optional[DiscountOffer]) -> None:
        if not offer or getattr(offer, "claimed_at", None):
            return
        await mark_offer_claimed(
            db,
            offer,
            details={
                "context": "fixed_price_tariff_purchase",
                "plan_code": self.PLAN_CODE,
                "period_days": self.PERIOD_DAYS,
                "price_kopeks": self.PRICE_KOPEKS,
            },
        )

    async def was_first_touch_sent_today(self, db: AsyncSession, user_id: int) -> bool:
        day_start, day_end = self._today_utc_window()
        result = await db.execute(
            select(func.count(InteractiveNotificationLog.id)).where(
                InteractiveNotificationLog.user_id == user_id,
                InteractiveNotificationLog.slot_key == self.FIRST_SLOT_KEY,
                InteractiveNotificationLog.status == "sent",
                InteractiveNotificationLog.created_at >= day_start,
                InteractiveNotificationLog.created_at < day_end,
            )
        )
        return int(result.scalar() or 0) > 0

    async def was_touch_sent_today(self, db: AsyncSession, user_id: int, slot_key: str) -> bool:
        day_start, day_end = self._today_utc_window()
        result = await db.execute(
            select(func.count(InteractiveNotificationLog.id)).where(
                InteractiveNotificationLog.user_id == user_id,
                InteractiveNotificationLog.slot_key == slot_key,
                InteractiveNotificationLog.status == "sent",
                InteractiveNotificationLog.created_at >= day_start,
                InteractiveNotificationLog.created_at < day_end,
            )
        )
        return int(result.scalar() or 0) > 0

    def _today_utc_window(self) -> tuple[datetime, datetime]:
        now_msk = datetime.now(self.MSK_TZ)
        start_msk = datetime.combine(now_msk.date(), datetime_time.min, tzinfo=self.MSK_TZ)
        end_msk = start_msk + timedelta(days=1)
        return (
            start_msk.astimezone(timezone.utc).replace(tzinfo=None),
            end_msk.astimezone(timezone.utc).replace(tzinfo=None),
        )

    def _to_msk(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(self.MSK_TZ)


cold_solo_offer_service = ColdSoloOfferService()
