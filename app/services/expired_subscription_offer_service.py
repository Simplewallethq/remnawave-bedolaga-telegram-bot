from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as datetime_time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.crud.discount_offer import mark_offer_claimed
from app.database.models import (
    DiscountOffer,
    InteractiveNotificationLog,
    Subscription,
    SubscriptionStatus,
    Transaction,
    TransactionType,
    User,
    UserStatus,
)
from app.services.hot_invoice_offer_service import hot_invoice_offer_service
from app.services.plan_pricing_service import SUPPORTED_PERIOD_DAYS
from app.utils.pricing_utils import apply_percentage_discount


@dataclass(frozen=True)
class ExpiredSubscriptionCandidate:
    subscription: Subscription
    user: User

    @property
    def campaign_key(self) -> str:
        ended_at = self.subscription.end_date.isoformat()
        return f"subscription:{self.subscription.id}:{ended_at}"

    @property
    def trigger_at(self) -> datetime:
        return self.subscription.end_date + timedelta(hours=1)

    @property
    def plan_code(self) -> Optional[str]:
        plan = getattr(self.subscription, "plan", None)
        return getattr(plan, "code", None)

    @property
    def plan_name(self) -> str:
        plan = getattr(self.subscription, "plan", None)
        return getattr(plan, "display_name", None) or self.plan_code or "тариф"


IS_ARTEM_DEBUG = True
ARTEM_DEBUG_USER_ID = 18835
ARTEM_DEBUG_TOUCH_INTERVAL = timedelta(minutes=5)


class ExpiredSubscriptionOfferService:
    NOTIFICATION_TYPE = "expired_subscription_return_20"
    EFFECT_TYPE = "percent_tariff_discount"
    DISCOUNT_PERCENT = 20

    FIRST_SLOT_KEY = "expired_subscription_c_1"
    SECOND_SLOT_KEY = "expired_subscription_c_2"
    THIRD_SLOT_KEY = "expired_subscription_c_3"
    FOURTH_MORNING_SLOT_KEY = "expired_subscription_c_4_1"
    FOURTH_EVENING_SLOT_KEY = "expired_subscription_c_4_2"
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
    FIRST_TOUCH_MIN_AGE = timedelta(hours=2)
    FIRST_TOUCH_MAX_AGE = timedelta(hours=2, minutes=5)

    def is_debug_enabled(self) -> bool:
        return IS_ARTEM_DEBUG

    def debug_touch_interval(self) -> timedelta:
        return ARTEM_DEBUG_TOUCH_INTERVAL

    def is_debug_user(self, user: Optional[User]) -> bool:
        if not IS_ARTEM_DEBUG or user is None:
            return False
        return bool(
            getattr(user, "id", None) == ARTEM_DEBUG_USER_ID
            or getattr(user, "telegram_id", None) == ARTEM_DEBUG_USER_ID
        )

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
    ) -> Optional[ExpiredSubscriptionCandidate]:
        user = await self.get_debug_user(db)
        if user is None:
            return None

        result = await db.execute(
            select(Subscription)
            .options(selectinload(Subscription.plan))
            .where(
                Subscription.user_id == user.id,
                Subscription.is_trial.is_(False),
                Subscription.is_partner.is_(False),
                Subscription.plan_id.isnot(None),
                Subscription.status.in_(
                    (SubscriptionStatus.ACTIVE.value, SubscriptionStatus.EXPIRED.value)
                ),
            )
            .order_by(Subscription.end_date.desc(), Subscription.id.desc())
            .limit(1)
        )
        subscription = result.scalar_one_or_none()
        if subscription is None:
            return None
        subscription.user = user
        return ExpiredSubscriptionCandidate(subscription=subscription, user=user)

    def first_touch_end_window(self, now_utc: datetime) -> tuple[datetime, datetime]:
        now = self._to_utc_naive(now_utc)
        return now - self.FIRST_TOUCH_MAX_AGE, now - self.FIRST_TOUCH_MIN_AGE

    def trigger_at(self, subscription: Subscription) -> datetime:
        return subscription.end_date + timedelta(hours=1)

    def campaign_expires_at(self, subscription: Subscription) -> datetime:
        user = getattr(subscription, "user", None)
        if IS_ARTEM_DEBUG and (
            getattr(subscription, "user_id", None) == ARTEM_DEBUG_USER_ID
            or getattr(user, "telegram_id", None) == ARTEM_DEBUG_USER_ID
        ):
            return datetime.utcnow() + timedelta(hours=2)

        trigger_msk = self._to_msk(self.trigger_at(subscription))
        expires_msk = datetime.combine(
            trigger_msk.date() + timedelta(days=6),
            datetime_time(hour=22),
            tzinfo=self.MSK_TZ,
        )
        return expires_msk.astimezone(timezone.utc).replace(tzinfo=None)

    def is_touch_due(
        self,
        subscription: Subscription,
        slot_key: str,
        now_utc: datetime,
    ) -> bool:
        now = self._to_utc_naive(now_utc)
        if slot_key == self.FIRST_SLOT_KEY:
            age = now - subscription.end_date
            return self.FIRST_TOUCH_MIN_AGE <= age < self.FIRST_TOUCH_MAX_AGE

        days_after = self.TOUCH_DAY_OFFSETS.get(slot_key)
        if days_after is None:
            return False
        trigger_date = self._to_msk(self.trigger_at(subscription)).date()
        return self._to_msk(now).date() == trigger_date + timedelta(days=days_after)

    def end_window_for_touch(
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
        after_subscription_id: int = 0,
        limit: int = 500,
    ) -> list[ExpiredSubscriptionCandidate]:
        ended_after, ended_before = self.first_touch_end_window(now_utc)
        return await self._list_candidates(
            db,
            ended_after=ended_after,
            ended_before=ended_before,
            after_subscription_id=after_subscription_id,
            limit=limit,
        )

    async def list_daily_touch_candidates(
        self,
        db: AsyncSession,
        slot_key: str,
        *,
        now_utc: datetime,
        after_subscription_id: int = 0,
        limit: int = 500,
    ) -> list[ExpiredSubscriptionCandidate]:
        window = self.end_window_for_touch(slot_key, now_utc)
        if window is None:
            return []
        ended_after, ended_before = window
        return await self._list_candidates(
            db,
            ended_after=ended_after,
            ended_before=ended_before,
            after_subscription_id=after_subscription_id,
            limit=limit,
        )

    async def _list_candidates(
        self,
        db: AsyncSession,
        *,
        ended_after: datetime,
        ended_before: datetime,
        after_subscription_id: int,
        limit: int,
    ) -> list[ExpiredSubscriptionCandidate]:
        result = await db.execute(
            select(Subscription, User)
            .join(User, User.id == Subscription.user_id)
            .options(selectinload(Subscription.plan))
            .where(
                Subscription.id > after_subscription_id,
                Subscription.end_date > ended_after,
                Subscription.end_date <= ended_before,
                Subscription.is_trial.is_(False),
                Subscription.is_partner.is_(False),
                Subscription.plan_id.isnot(None),
                Subscription.status.in_(
                    (SubscriptionStatus.ACTIVE.value, SubscriptionStatus.EXPIRED.value)
                ),
                User.telegram_id.isnot(None),
                User.status == UserStatus.ACTIVE.value,
                User.has_had_paid_subscription.is_(True),
            )
            .order_by(Subscription.id.asc())
            .limit(limit)
        )
        return [
            ExpiredSubscriptionCandidate(subscription=row[0], user=row[1])
            for row in result.all()
        ]

    async def get_subscription(
        self,
        db: AsyncSession,
        subscription_id: int,
    ) -> Optional[Subscription]:
        result = await db.execute(
            select(Subscription)
            .options(selectinload(Subscription.user), selectinload(Subscription.plan))
            .where(Subscription.id == subscription_id)
        )
        return result.scalar_one_or_none()

    async def has_subscription_payment_after(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        ended_at: datetime,
    ) -> bool:
        result = await db.execute(
            select(func.count(Transaction.id)).where(
                Transaction.user_id == user_id,
                Transaction.is_completed.is_(True),
                Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
                func.coalesce(Transaction.completed_at, Transaction.created_at)
                >= ended_at,
            )
        )
        return int(result.scalar() or 0) > 0

    async def is_campaign_eligible(
        self,
        db: AsyncSession,
        candidate: ExpiredSubscriptionCandidate,
        *,
        now_utc: Optional[datetime] = None,
    ) -> bool:
        subscription = candidate.subscription
        user = candidate.user
        if not user.telegram_id or user.status != UserStatus.ACTIVE.value:
            return False
        if (
            subscription.is_trial
            or subscription.is_partner
            or subscription.plan_id is None
        ):
            return False
        if self.is_debug_user(user):
            return True
        if not getattr(user, "has_had_paid_subscription", False):
            return False
        if subscription.end_date > self._to_utc_naive(now_utc or datetime.utcnow()):
            return False
        if await self.has_subscription_payment_after(
            db,
            user_id=user.id,
            ended_at=subscription.end_date,
        ):
            return False
        if await hot_invoice_offer_service.has_unpaid_invoice_since(
            db,
            user_id=user.id,
            since=subscription.end_date,
        ):
            return False
        if (
            await hot_invoice_offer_service.get_active_campaign_payment_id(
                db,
                user_id=user.id,
                now_utc=now_utc or datetime.utcnow(),
            )
            is not None
        ):
            return False
        return True

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
                DiscountOffer.expires_at > datetime.utcnow(),
            )
            .limit(1)
        )
        return result.scalar_one_or_none() is not None

    async def ensure_discount_offer(
        self,
        db: AsyncSession,
        candidate: ExpiredSubscriptionCandidate,
    ) -> DiscountOffer:
        existing = await self.get_available_offer(db, candidate.user.id)
        if existing:
            return existing

        expires_at = self.campaign_expires_at(candidate.subscription)
        offer = DiscountOffer(
            user_id=candidate.user.id,
            subscription_id=candidate.subscription.id,
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
        if offer is None or not validate_campaign:
            return offer

        try:
            subscription_id = int((offer.extra_data or {}).get("subscription_id") or 0)
        except (TypeError, ValueError):
            subscription_id = 0
        subscription = (
            await self.get_subscription(db, subscription_id)
            if subscription_id
            else None
        )
        user = getattr(subscription, "user", None) if subscription else None
        if subscription is None or user is None:
            offer.is_active = False
            await db.commit()
            return None
        candidate = ExpiredSubscriptionCandidate(subscription=subscription, user=user)
        if not await self.is_campaign_eligible(db, candidate):
            offer.is_active = False
            await db.commit()
            return None
        return offer

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
        if base_price_kopeks is None or period_days not in SUPPORTED_PERIOD_DAYS:
            return None, None

        offer = await self.get_available_offer(
            db,
            user_id,
            validate_campaign=validate_campaign,
        )
        if not offer:
            return None, None
        extra = offer.extra_data or {}
        if str(extra.get("plan_code") or "").lower() != (plan_code or "").lower():
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
                "context": "expired_subscription_return_tariff_purchase",
                "plan_code": plan_code,
                "period_days": period_days,
                "base_price_kopeks": base_price_kopeks,
                "price_kopeks": price_kopeks,
            },
        )

    async def deactivate_offer(self, db: AsyncSession, offer: DiscountOffer) -> None:
        offer.is_active = False
        await db.commit()

    def build_campaign_payload(self, candidate: ExpiredSubscriptionCandidate) -> dict:
        return {
            "subscription_id": candidate.subscription.id,
            "campaign_key": candidate.campaign_key,
            "ended_at": candidate.subscription.end_date.isoformat(),
            "trigger_at": candidate.trigger_at.isoformat(),
            "plan_id": candidate.subscription.plan_id,
            "plan_code": candidate.plan_code,
            "plan_name": candidate.plan_name,
        }

    def _to_msk(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(self.MSK_TZ)

    @staticmethod
    def _to_utc_naive(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value
        return value.astimezone(timezone.utc).replace(tzinfo=None)


expired_subscription_offer_service = ExpiredSubscriptionOfferService()
