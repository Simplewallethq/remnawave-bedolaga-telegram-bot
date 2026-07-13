from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, time as datetime_time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.discount_offer import mark_offer_claimed
from app.database.models import (
    DiscountOffer,
    InteractiveNotificationLog,
    PlategaPayment,
    Transaction,
    TransactionType,
    User,
    UserStatus,
)
from app.utils.pricing_utils import apply_percentage_discount


@dataclass(frozen=True)
class HotInvoiceCandidate:
    payment: PlategaPayment
    user: User

    @property
    def campaign_key(self) -> str:
        return f"platega:{self.payment.id}"

    @property
    def trigger_at(self) -> datetime:
        return self.payment.created_at + timedelta(hours=1)


IS_ARTEM_DEBUG = True
ARTEM_DEBUG_USER_ID = 18835
ARTEM_DEBUG_TOUCH_INTERVAL = timedelta(minutes=5)


class HotInvoiceOfferService:
    NOTIFICATION_TYPE = "hot_invoice_20"
    EFFECT_TYPE = "percent_tariff_discount"
    DISCOUNT_PERCENT = 20

    FIRST_SLOT_KEY = "hot_invoice_b_1"
    SECOND_SLOT_KEY = "hot_invoice_b_2"
    THIRD_SLOT_KEY = "hot_invoice_b_3"
    FOURTH_MORNING_SLOT_KEY = "hot_invoice_b_4_1"
    FOURTH_EVENING_SLOT_KEY = "hot_invoice_b_4_2"
    SLOT_KEYS = (
        FIRST_SLOT_KEY,
        SECOND_SLOT_KEY,
        THIRD_SLOT_KEY,
        FOURTH_MORNING_SLOT_KEY,
        FOURTH_EVENING_SLOT_KEY,
    )
    TOUCH_DAY_OFFSETS = {
        SECOND_SLOT_KEY: 1,
        THIRD_SLOT_KEY: 3,
        FOURTH_MORNING_SLOT_KEY: 6,
        FOURTH_EVENING_SLOT_KEY: 6,
    }

    MSK_TZ = ZoneInfo("Europe/Moscow")
    FIRST_TOUCH_MIN_AGE = timedelta(minutes=50)
    FIRST_TOUCH_MAX_AGE = timedelta(minutes=55)

    def is_debug_enabled(self) -> bool:
        return IS_ARTEM_DEBUG

    def debug_touch_interval(self) -> timedelta:
        return ARTEM_DEBUG_TOUCH_INTERVAL

    async def get_debug_user(self, db: AsyncSession) -> Optional[User]:
        if not IS_ARTEM_DEBUG:
            return None
        result = await db.execute(
            select(User)
            .where(
                (User.id == ARTEM_DEBUG_USER_ID)
                | (User.telegram_id == ARTEM_DEBUG_USER_ID)
            )
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def get_debug_candidate(
        self,
        db: AsyncSession,
    ) -> Optional[HotInvoiceCandidate]:
        user = await self.get_debug_user(db)
        if user is None:
            return None

        result = await db.execute(
            select(PlategaPayment)
            .where(PlategaPayment.user_id == user.id)
            .order_by(PlategaPayment.id.desc())
            .limit(1)
        )
        payment = result.scalar_one_or_none()
        if payment is None:
            return None
        return HotInvoiceCandidate(payment=payment, user=user)

    async def is_debug_payment(
        self,
        db: AsyncSession,
        payment: PlategaPayment,
    ) -> bool:
        user = await self.get_debug_user(db)
        return bool(IS_ARTEM_DEBUG and user is not None and payment.user_id == user.id)

    def first_touch_created_window(
        self, now_utc: datetime
    ) -> tuple[datetime, datetime]:
        now = self._to_utc_naive(now_utc)
        return now - self.FIRST_TOUCH_MAX_AGE, now - self.FIRST_TOUCH_MIN_AGE

    def trigger_at(self, payment: PlategaPayment) -> datetime:
        return payment.created_at + timedelta(hours=1)

    def campaign_expires_at(self, payment: PlategaPayment) -> datetime:
        if IS_ARTEM_DEBUG and payment.user_id == ARTEM_DEBUG_USER_ID:
            return datetime.utcnow() + timedelta(hours=2)
        trigger_msk = self._to_msk(self.trigger_at(payment))
        expires_msk = datetime.combine(
            trigger_msk.date() + timedelta(days=6),
            datetime_time(hour=22),
            tzinfo=self.MSK_TZ,
        )
        return expires_msk.astimezone(timezone.utc).replace(tzinfo=None)

    def invoice_minutes_left(
        self,
        payment: PlategaPayment,
        now_utc: datetime,
    ) -> int:
        if IS_ARTEM_DEBUG and payment.user_id == ARTEM_DEBUG_USER_ID:
            return 10
        now = self._to_utc_naive(now_utc)
        expires_at = payment.expires_at or payment.created_at + timedelta(hours=1)
        seconds_left = (self._to_utc_naive(expires_at) - now).total_seconds()
        return max(0, math.ceil(seconds_left / 60))

    def is_touch_due(
        self,
        payment: PlategaPayment,
        slot_key: str,
        now_utc: datetime,
    ) -> bool:
        now = self._to_utc_naive(now_utc)
        if slot_key == self.FIRST_SLOT_KEY:
            age = now - payment.created_at
            return self.FIRST_TOUCH_MIN_AGE <= age < self.FIRST_TOUCH_MAX_AGE

        days_after = self.TOUCH_DAY_OFFSETS.get(slot_key)
        if days_after is None:
            return False
        trigger_date = self._to_msk(self.trigger_at(payment)).date()
        return self._to_msk(now).date() == trigger_date + timedelta(days=days_after)

    def invoice_created_window_for_touch(
        self,
        slot_key: str,
        now_utc: datetime,
    ) -> Optional[tuple[datetime, datetime]]:
        days_after = self.TOUCH_DAY_OFFSETS.get(slot_key)
        if days_after is None:
            return None

        target_trigger_date = self._to_msk(now_utc).date() - timedelta(days=days_after)
        trigger_start_msk = datetime.combine(
            target_trigger_date,
            datetime_time.min,
            tzinfo=self.MSK_TZ,
        )
        trigger_end_msk = trigger_start_msk + timedelta(days=1)
        return (
            (trigger_start_msk.astimezone(timezone.utc) - timedelta(hours=1)).replace(
                tzinfo=None
            ),
            (trigger_end_msk.astimezone(timezone.utc) - timedelta(hours=1)).replace(
                tzinfo=None
            ),
        )

    async def list_first_touch_candidates(
        self,
        db: AsyncSession,
        *,
        now_utc: datetime,
        after_payment_id: int = 0,
        limit: int = 500,
    ) -> list[HotInvoiceCandidate]:
        created_after, created_before = self.first_touch_created_window(now_utc)
        payment_after_invoice = self._payment_after_invoice_exists()
        result = await db.execute(
            select(PlategaPayment, User)
            .join(User, User.id == PlategaPayment.user_id)
            .where(
                PlategaPayment.id > after_payment_id,
                PlategaPayment.created_at > created_after,
                PlategaPayment.created_at <= created_before,
                PlategaPayment.is_paid.is_(False),
                PlategaPayment.redirect_url.isnot(None),
                (
                    PlategaPayment.expires_at.is_(None)
                    | (PlategaPayment.expires_at > self._to_utc_naive(now_utc))
                ),
                User.telegram_id.isnot(None),
                User.status == UserStatus.ACTIVE.value,
                ~payment_after_invoice,
            )
            .order_by(PlategaPayment.id.asc())
            .limit(limit)
        )
        return [
            HotInvoiceCandidate(payment=row[0], user=row[1]) for row in result.all()
        ]

    async def list_daily_touch_candidates(
        self,
        db: AsyncSession,
        slot_key: str,
        *,
        now_utc: datetime,
        after_payment_id: int = 0,
        limit: int = 500,
    ) -> list[HotInvoiceCandidate]:
        window = self.invoice_created_window_for_touch(slot_key, now_utc)
        if window is None:
            return []
        created_after, created_before = window
        payment_after_invoice = self._payment_after_invoice_exists()

        latest_ids = (
            select(func.max(PlategaPayment.id).label("payment_id"))
            .where(
                PlategaPayment.created_at >= created_after,
                PlategaPayment.created_at < created_before,
                PlategaPayment.is_paid.is_(False),
                ~payment_after_invoice,
            )
            .group_by(PlategaPayment.user_id)
            .subquery()
        )
        result = await db.execute(
            select(PlategaPayment, User)
            .join(latest_ids, latest_ids.c.payment_id == PlategaPayment.id)
            .join(User, User.id == PlategaPayment.user_id)
            .where(
                PlategaPayment.id > after_payment_id,
                User.telegram_id.isnot(None),
                User.status == UserStatus.ACTIVE.value,
            )
            .order_by(PlategaPayment.id.asc())
            .limit(limit)
        )
        return [
            HotInvoiceCandidate(payment=row[0], user=row[1]) for row in result.all()
        ]

    async def get_payment(
        self, db: AsyncSession, payment_id: int
    ) -> Optional[PlategaPayment]:
        result = await db.execute(
            select(PlategaPayment).where(PlategaPayment.id == payment_id)
        )
        return result.scalar_one_or_none()

    async def has_completed_payment_after(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        created_at: datetime,
    ) -> bool:
        result = await db.execute(
            select(func.count(Transaction.id)).where(
                Transaction.user_id == user_id,
                Transaction.is_completed.is_(True),
                Transaction.type.in_(
                    (
                        TransactionType.DEPOSIT.value,
                        TransactionType.SUBSCRIPTION_PAYMENT.value,
                    )
                ),
                func.coalesce(Transaction.completed_at, Transaction.created_at)
                >= created_at,
            )
        )
        return int(result.scalar() or 0) > 0

    async def is_campaign_eligible(
        self,
        db: AsyncSession,
        payment: PlategaPayment,
    ) -> bool:
        if await self.is_debug_payment(db, payment):
            return True
        if payment.is_paid:
            return False
        return not await self.has_completed_payment_after(
            db,
            user_id=payment.user_id,
            created_at=payment.created_at,
        )

    async def has_unpaid_invoice_since(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        since: datetime,
    ) -> bool:
        payment_after_invoice = self._payment_after_invoice_exists()
        result = await db.execute(
            select(PlategaPayment.id)
            .where(
                PlategaPayment.user_id == user_id,
                PlategaPayment.created_at >= since,
                PlategaPayment.is_paid.is_(False),
                ~payment_after_invoice,
            )
            .limit(1)
        )
        return result.scalar_one_or_none() is not None

    async def was_touch_sent(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        slot_key: str,
        campaign_key: str,
    ) -> bool:
        result = await db.execute(
            select(InteractiveNotificationLog.payload).where(
                InteractiveNotificationLog.user_id == user_id,
                InteractiveNotificationLog.slot_key == slot_key,
                InteractiveNotificationLog.status == "sent",
            )
        )
        return any(
            isinstance(payload, dict) and payload.get("campaign_key") == campaign_key
            for payload in result.scalars().all()
        )

    async def get_active_campaign_payment_id(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        now_utc: datetime,
    ) -> Optional[int]:
        result = await db.execute(
            select(InteractiveNotificationLog.payload)
            .where(
                InteractiveNotificationLog.user_id == user_id,
                InteractiveNotificationLog.slot_key.in_(self.SLOT_KEYS),
                InteractiveNotificationLog.status == "sent",
                InteractiveNotificationLog.created_at
                >= self._to_utc_naive(now_utc) - timedelta(days=7, hours=2),
            )
            .order_by(InteractiveNotificationLog.created_at.desc())
        )
        for payload in result.scalars().all():
            if not isinstance(payload, dict):
                continue
            payment_id = payload.get("payment_id")
            trigger_raw = payload.get("trigger_at")
            try:
                trigger_at = datetime.fromisoformat(str(trigger_raw))
                payment_id = int(payment_id)
            except (TypeError, ValueError):
                continue
            trigger_msk = self._to_msk(trigger_at)
            campaign_end_msk = datetime.combine(
                trigger_msk.date() + timedelta(days=6),
                datetime_time(hour=22),
                tzinfo=self.MSK_TZ,
            )
            if self._to_msk(now_utc) < campaign_end_msk:
                return payment_id
        return None

    async def ensure_discount_offer(
        self,
        db: AsyncSession,
        candidate: HotInvoiceCandidate,
    ) -> DiscountOffer:
        existing = await self.get_available_offer(db, candidate.user.id)
        if existing:
            return existing

        expires_at = self.campaign_expires_at(candidate.payment)
        offer = DiscountOffer(
            user_id=candidate.user.id,
            subscription_id=None,
            notification_type=self.NOTIFICATION_TYPE,
            discount_percent=self.DISCOUNT_PERCENT,
            bonus_amount_kopeks=0,
            expires_at=expires_at,
            claimed_at=None,
            is_active=True,
            effect_type=self.EFFECT_TYPE,
            extra_data={
                **self.build_campaign_payload(candidate),
                "discount_expires_at": expires_at.isoformat(),
                "activated_at": None,
            },
        )
        db.add(offer)
        await db.commit()
        await db.refresh(offer)
        return offer

    async def get_available_offer(
        self,
        db: AsyncSession,
        user_id: int,
        *,
        validate_campaign: bool = True,
    ) -> Optional[DiscountOffer]:
        result = await db.execute(
            select(DiscountOffer)
            .where(
                DiscountOffer.user_id == user_id,
                DiscountOffer.notification_type == self.NOTIFICATION_TYPE,
                DiscountOffer.effect_type == self.EFFECT_TYPE,
                DiscountOffer.is_active.is_(True),
                DiscountOffer.claimed_at.is_(None),
                DiscountOffer.expires_at > datetime.utcnow(),
            )
            .order_by(DiscountOffer.expires_at.asc())
        )
        offer = result.scalars().first()
        if offer is None:
            return None

        if not validate_campaign:
            return offer

        try:
            payment_id = int((offer.extra_data or {}).get("payment_id") or 0)
        except (TypeError, ValueError):
            payment_id = 0
        payment = await self.get_payment(db, payment_id) if payment_id else None
        if payment is None or not await self.is_campaign_eligible(db, payment):
            offer.is_active = False
            await db.commit()
            return None
        return offer

    async def has_conflicting_active_offer(
        self,
        db: AsyncSession,
        user_id: int,
    ) -> bool:
        result = await db.execute(
            select(DiscountOffer.id)
            .where(
                DiscountOffer.user_id == user_id,
                DiscountOffer.notification_type != self.NOTIFICATION_TYPE,
                DiscountOffer.is_active.is_(True),
                DiscountOffer.claimed_at.is_(None),
                DiscountOffer.expires_at
                > datetime.now(timezone.utc).replace(tzinfo=None),
            )
            .limit(1)
        )
        return result.scalar_one_or_none() is not None

    async def activate_offer(
        self,
        db: AsyncSession,
        offer: DiscountOffer,
    ) -> DiscountOffer:
        extra_data = dict(offer.extra_data or {})
        if not extra_data.get("activated_at"):
            extra_data["activated_at"] = datetime.utcnow().isoformat()
            offer.extra_data = extra_data
            await db.commit()
            await db.refresh(offer)
        return offer

    @staticmethod
    def is_offer_activated(offer: Optional[DiscountOffer]) -> bool:
        return bool(offer and (offer.extra_data or {}).get("activated_at"))

    async def get_price_override(
        self,
        db: AsyncSession,
        user_id: int,
        *,
        plan_code: str,
        period_days: int,
        base_price_kopeks: Optional[int],
        activate: bool = False,
        validate_campaign: bool = True,
    ) -> tuple[Optional[int], Optional[DiscountOffer]]:
        if base_price_kopeks is None:
            return None, None

        offer = await self.get_available_offer(
            db,
            user_id,
            validate_campaign=validate_campaign,
        )
        if not offer:
            return None, None
        if activate:
            offer = await self.activate_offer(db, offer)
        discounted_price, _ = apply_percentage_discount(
            int(base_price_kopeks),
            self.DISCOUNT_PERCENT,
        )
        return discounted_price, offer

    async def mark_claimed_after_purchase(
        self,
        db: AsyncSession,
        offer: Optional[DiscountOffer],
        *,
        plan_code: str,
        period_days: int,
        base_price_kopeks: int,
        price_kopeks: int,
    ) -> None:
        if not offer or offer.claimed_at:
            return
        await mark_offer_claimed(
            db,
            offer,
            details={
                "context": "hot_invoice_tariff_purchase",
                "plan_code": plan_code,
                "period_days": period_days,
                "base_price_kopeks": base_price_kopeks,
                "price_kopeks": price_kopeks,
            },
        )

    async def deactivate_offer(self, db: AsyncSession, offer: DiscountOffer) -> None:
        offer.is_active = False
        await db.commit()

    def build_campaign_payload(self, candidate: HotInvoiceCandidate) -> dict:
        return {
            "provider": "platega",
            "payment_id": candidate.payment.id,
            "campaign_key": candidate.campaign_key,
            "invoice_created_at": candidate.payment.created_at.isoformat(),
            "trigger_at": candidate.trigger_at.isoformat(),
        }

    def _payment_after_invoice_exists(self):
        return (
            select(Transaction.id)
            .where(
                Transaction.user_id == PlategaPayment.user_id,
                Transaction.is_completed.is_(True),
                Transaction.type.in_(
                    (
                        TransactionType.DEPOSIT.value,
                        TransactionType.SUBSCRIPTION_PAYMENT.value,
                    )
                ),
                func.coalesce(Transaction.completed_at, Transaction.created_at)
                >= PlategaPayment.created_at,
            )
            .exists()
        )

    def _to_msk(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(self.MSK_TZ)

    @staticmethod
    def _to_utc_naive(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value
        return value.astimezone(timezone.utc).replace(tzinfo=None)


hot_invoice_offer_service = HotInvoiceOfferService()
