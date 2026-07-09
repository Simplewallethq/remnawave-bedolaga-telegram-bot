from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.crud.discount_offer import mark_offer_claimed, upsert_discount_offer
from app.database.models import DiscountOffer, User, UserStatus


logger = logging.getLogger(__name__)


IS_ARTEM_DEBUG = False
ARTEM_DEBUG_USER_ID = 18835


@dataclass(frozen=True)
class LegacyProOfferMember:
    telegram_id: int
    username: Optional[str]
    first_name: Optional[str]
    group: str
    payments: int
    total_rub: float
    last_monthly_price: Optional[float]
    last_payment: Optional[str]
    offer_year_pro_rub: int
    offer_per_month: int

    @property
    def price_kopeks(self) -> int:
        return self.offer_year_pro_rub * 100


@dataclass(frozen=True)
class LegacyProOfferMessage:
    slot_key: str
    text: str
    button_text: str
    groups: tuple[str, ...]


class LegacyProOfferService:
    """Fixed-price migration offers for legacy paid users from CSV.

    A/C users get Pro for 360 days at 1290 RUB. B users get Pro for 360 days
    at 990 RUB. D is intentionally ignored until a separate task.
    """

    EFFECT_TYPE = "fixed_price_tariff"
    PLAN_CODE = "pro"
    PERIOD_DAYS = 360
    ORIGINAL_PRICE_KOPEKS = 228_000
    MSK_TZ = ZoneInfo("Europe/Moscow")
    CSV_PATH = Path(__file__).resolve().parents[1] / "data" / "legacy_groups_offers.csv"

    NOTIFICATION_TYPE_1290 = "legacy_pro_1290"
    NOTIFICATION_TYPE_990 = "legacy_pro_990"
    NOTIFICATION_TYPES = (NOTIFICATION_TYPE_1290, NOTIFICATION_TYPE_990)

    GROUP_A = "A_лояльные"
    GROUP_B = "B_скидочники"
    GROUP_C = "C_полноценники"
    GROUP_D = "D_спящие"
    AC_GROUPS = (GROUP_A, GROUP_C)
    B_GROUPS = (GROUP_B,)

    AC_SLOT_1 = "legacy_pro_1290_ac_1"
    AC_SLOT_2 = "legacy_pro_1290_ac_2"
    AC_SLOT_3 = "legacy_pro_1290_ac_3"
    AC_SLOT_4 = "legacy_pro_1290_ac_4"
    B_SLOT_1 = "legacy_pro_990_b_1"
    B_SLOT_2 = "legacy_pro_990_b_2"
    B_SLOT_3 = "legacy_pro_990_b_3"
    B_SLOT_4 = "legacy_pro_990_b_4"

    OFFER_EXPIRES_AT_MSK = datetime(2026, 8, 1, 23, 59, 59, tzinfo=MSK_TZ)

    # AC_SCHEDULE = (
    #     # (AC_SLOT_1, datetime(2026, 7, 9, 11, 20, tzinfo=MSK_TZ)),
    #     # (AC_SLOT_2, datetime(2026, 7, 9, 11, 25, tzinfo=MSK_TZ)),
    #     # (AC_SLOT_3, datetime(2026, 7, 9, 11, 30, tzinfo=MSK_TZ)),
    #     # (AC_SLOT_4, datetime(2026, 7, 9, 11, 35, tzinfo=MSK_TZ)),
    # )
    # B_SCHEDULE = (
    #     (B_SLOT_1, datetime(2026, 7, 9, 12, 7, tzinfo=MSK_TZ)),
    #     (B_SLOT_2, datetime(2026, 7, 9, 12, 8, tzinfo=MSK_TZ)),
    #     (B_SLOT_3, datetime(2026, 7, 9, 12, 9, tzinfo=MSK_TZ)),
    #     (B_SLOT_4, datetime(2026, 7, 9, 12, 10, tzinfo=MSK_TZ)),
    # )

    AC_SCHEDULE = (
        (AC_SLOT_1, datetime(2026, 7, 9, 13, 30, tzinfo=MSK_TZ)),
        (AC_SLOT_2, datetime(2026, 7, 17, 21, 30, tzinfo=MSK_TZ)),
        (AC_SLOT_3, datetime(2026, 7, 24, 21, 30, tzinfo=MSK_TZ)),
        (AC_SLOT_4, datetime(2026, 7, 30, 21, 30, tzinfo=MSK_TZ)),
    )
    B_SCHEDULE = (
        (B_SLOT_1, datetime(2026, 7, 9, 13, 30, tzinfo=MSK_TZ)),
        (B_SLOT_2, datetime(2026, 7, 17, 21, 30, tzinfo=MSK_TZ)),
        (B_SLOT_3, datetime(2026, 7, 24, 21, 30, tzinfo=MSK_TZ)),
        (B_SLOT_4, datetime(2026, 7, 30, 21, 30, tzinfo=MSK_TZ)),
    )

    AC_MESSAGES = {
        AC_SLOT_1: LegacyProOfferMessage(
            slot_key=AC_SLOT_1,
            groups=AC_GROUPS,
            button_text="💎 Забрать год Pro за 1290₽",
            text=(
                "<b>Время зафиксировать выгоду на Leto VPN 👇</b>\n\n"
                "Уже два месяца у нас действуют новые тарифы для новых пользователей.\n\n"
                "С 1 августа мы переводим всех пользователей на единую тарифную сетку:\n"
                "Solo · 1 устр · 320₽/мес — Plus · 3 устр · 490₽/мес — Pro · 10 устр · 690₽/мес\n\n"
                "Ты был с нами с самого начала — спасибо за это! Поэтому у нас для тебя персональное предложение:\n\n"
                "<b>💎 1 год на тарифе Pro (10 устр) за 1290₽*</b>\n"
                "Это 107₽/мес — дешевле, чем ты платишь сейчас.\n\n"
                "Для всех остальных Pro на год стоит 2280₽.\n\n"
                "⏰ Предложение действует до <b>1 августа</b> — в этот день твой старый тариф закроется, останутся только обычные.\n\n"
                "*спец-цена действует на первый год"
            ),
        ),
        AC_SLOT_2: LegacyProOfferMessage(
            slot_key=AC_SLOT_2,
            groups=AC_GROUPS,
            button_text="💎 Забрать год Pro за 1290₽",
            text=(
                "<b>Напоминаем: 1 августа твой старый тариф закроется</b>\n\n"
                "Твоё персональное предложение ещё в силе:\n\n"
                "<b>💎 Год Pro (10 устр) за 1290₽ — это 107₽/мес.</b>\n"
                "Для всех остальных он стоит 2280₽."
            ),
        ),
        AC_SLOT_3: LegacyProOfferMessage(
            slot_key=AC_SLOT_3,
            groups=AC_GROUPS,
            button_text="💎 Забрать за 1290₽",
            text=(
                "<b>⏰ Осталась неделя</b>\n\n"
                "<b>1 августа</b> твой текущий тариф закроется, и вместе с ним сгорит твоя персональная цена:\n\n"
                "💎 Год Pro за 1290₽ вместо 2280₽."
            ),
        ),
        AC_SLOT_4: LegacyProOfferMessage(
            slot_key=AC_SLOT_4,
            groups=AC_GROUPS,
            button_text="💎 Забрать Pro за 1290₽",
            text=(
                "<b>⏰ Завтра — последний день</b>\n\n"
                "Твой тариф закрывается, персональная цена сгорает завтра в 23:59 MSK.\n\n"
                "💎 Год Pro за 1290₽ — больше такого предложения не будет."
            ),
        ),
    }
    B_MESSAGES = {
        B_SLOT_1: LegacyProOfferMessage(
            slot_key=B_SLOT_1,
            groups=B_GROUPS,
            button_text="💎 Забрать год Pro за 990₽",
            text=(
                "<b>Время зафиксировать выгоду на Leto VPN 👇</b>\n\n"
                "Уже два месяца у нас действуют новые тарифы для новых пользователей.\n\n"
                "С 1 августа мы переводим всех пользователей на единую тарифную сетку:\n"
                "Solo · 1 устр · 320₽/мес — Plus · 3 устр · 490₽/мес — Pro · 10 устр · 690₽/мес\n\n"
                "Ты был с нами с самого начала — спасибо за это! Поэтому у нас для тебя персональное предложение:\n\n"
                "<b>💎 1 год на тарифе Pro (10 устр) за 990₽*</b>\n"
                "Это 82₽/мес — дешевле, чем ты платишь сейчас.\n\n"
                "Для всех остальных Pro на год стоит 2280₽.\n\n"
                "⏰ Предложение действует до <b>1 августа</b> — в этот день твой старый тариф закроется, останутся только обычные.\n\n"
                "*спец-цена действует на первый год"
            ),
        ),
        B_SLOT_2: LegacyProOfferMessage(
            slot_key=B_SLOT_2,
            groups=B_GROUPS,
            button_text="💎 Забрать год Pro за 990₽",
            text=(
                "<b>Напоминаем: 1 августа твой старый тариф закроется</b>\n\n"
                "Твоё персональное предложение ещё в силе:\n\n"
                "<b>💎 Год Pro (10 устр) за 990₽ — это 82₽/мес.</b>\n"
                "Для всех остальных он стоит 2280₽."
            ),
        ),
        B_SLOT_3: LegacyProOfferMessage(
            slot_key=B_SLOT_3,
            groups=B_GROUPS,
            button_text="💎 Забрать за 990₽",
            text=(
                "<b>⏰ Осталась неделя</b>\n\n"
                "<b>1 августа</b> твой текущий тариф закроется, и вместе с ним сгорит твоя персональная цена:\n\n"
                "💎 Год Pro за 990₽ вместо 2280₽."
            ),
        ),
        B_SLOT_4: LegacyProOfferMessage(
            slot_key=B_SLOT_4,
            groups=B_GROUPS,
            button_text="💎 Забрать Pro за 990₽",
            text=(
                "<b>⏰ Завтра — последний день</b>\n\n"
                "Твой тариф закрывается, персональная цена сгорает завтра в 23:59 MSK.\n\n"
                "💎 Год Pro за 990₽ — больше такого предложения не будет."
            ),
        ),
    }

    def get_one_off_schedule(self) -> tuple[tuple[str, datetime], ...]:
        return self.AC_SCHEDULE + self.B_SCHEDULE

    def get_message_for_slot(self, slot_key: str) -> Optional[LegacyProOfferMessage]:
        return self.AC_MESSAGES.get(slot_key) or self.B_MESSAGES.get(slot_key)

    def offer_expires_at(self) -> datetime:
        return self.OFFER_EXPIRES_AT_MSK.astimezone(timezone.utc).replace(tzinfo=None)

    def notification_type_for_price(self, price_kopeks: int) -> Optional[str]:
        if price_kopeks == 129_000:
            return self.NOTIFICATION_TYPE_1290
        if price_kopeks == 99_000:
            return self.NOTIFICATION_TYPE_990
        return None

    def notification_type_for_member(
        self, member: LegacyProOfferMember
    ) -> Optional[str]:
        return self.notification_type_for_price(member.price_kopeks)

    def is_supported_member(self, member: LegacyProOfferMember) -> bool:
        if member.group == self.GROUP_D:
            return False
        return self.notification_type_for_member(member) is not None

    def is_debug_enabled(self) -> bool:
        return bool(IS_ARTEM_DEBUG)

    def debug_user_id(self) -> int:
        return int(ARTEM_DEBUG_USER_ID)

    def build_debug_member(
        self,
        user: User,
        groups: Iterable[str],
    ) -> Optional[LegacyProOfferMember]:
        telegram_id = getattr(user, "telegram_id", None)
        if not telegram_id:
            return None

        group_set = set(groups or [])
        if self.GROUP_B in group_set:
            group = self.GROUP_B
            offer_year_pro_rub = 990
            offer_per_month = 82
        else:
            group = self.GROUP_A
            offer_year_pro_rub = 1290
            offer_per_month = 107

        return LegacyProOfferMember(
            telegram_id=int(telegram_id),
            username=getattr(user, "username", None),
            first_name=getattr(user, "first_name", None),
            group=group,
            payments=0,
            total_rub=0.0,
            last_monthly_price=None,
            last_payment=None,
            offer_year_pro_rub=offer_year_pro_rub,
            offer_per_month=offer_per_month,
        )

    def list_members(
        self, groups: Optional[Iterable[str]] = None
    ) -> list[LegacyProOfferMember]:
        group_filter = set(groups or [])
        members: list[LegacyProOfferMember] = []

        try:
            with self.CSV_PATH.open("r", newline="", encoding="utf-8-sig") as csv_file:
                reader = csv.DictReader(csv_file)
                for row in reader:
                    member = self._member_from_row(row)
                    if member is None:
                        continue
                    if group_filter and member.group not in group_filter:
                        continue
                    if not self.is_supported_member(member):
                        continue
                    members.append(member)
        except FileNotFoundError:
            logger.error("Legacy Pro offer CSV not found: %s", self.CSV_PATH)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Failed to read Legacy Pro offer CSV %s: %s", self.CSV_PATH, exc
            )

        return members

    def get_member_by_telegram_id(
        self, telegram_id: int
    ) -> Optional[LegacyProOfferMember]:
        for member in self.list_members():
            if member.telegram_id == int(telegram_id):
                return member
        return None

    def build_extra_data(self, member: LegacyProOfferMember) -> dict[str, Any]:
        price_kopeks = member.price_kopeks
        price_rub = member.offer_year_pro_rub
        return {
            "offer_type": self.notification_type_for_member(member),
            "campaign": "legacy_groups_pro_2026_07",
            "plan_code": self.PLAN_CODE,
            "period_days": self.PERIOD_DAYS,
            "price_kopeks": price_kopeks,
            "original_price_kopeks": self.ORIGINAL_PRICE_KOPEKS,
            "title": f"1 год Pro за {price_rub}₽",
            "button_text": f"Забрать Pro за {price_rub}₽",
            "message_text": f"Pro на 1 год за {price_rub}₽ вместо 2280₽.",
            "csv_group": member.group,
            "telegram_id": member.telegram_id,
            "offer_per_month": member.offer_per_month,
            "expires_at_msk": self.OFFER_EXPIRES_AT_MSK.isoformat(),
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
        if getattr(offer, "notification_type", None) not in self.NOTIFICATION_TYPES:
            return False
        if getattr(offer, "effect_type", None) != self.EFFECT_TYPE:
            return False
        if not getattr(offer, "is_active", False) or getattr(offer, "claimed_at", None):
            return False
        expires_at = getattr(offer, "expires_at", None)
        if not expires_at or expires_at <= datetime.utcnow():
            return False

        extra = getattr(offer, "extra_data", None) or {}
        return str(extra.get("plan_code") or self.PLAN_CODE).lower() == (
            plan_code or ""
        ).lower() and int(extra.get("period_days") or self.PERIOD_DAYS) == int(
            period_days or 0
        )

    async def get_active_offer(
        self, db: AsyncSession, user_id: int
    ) -> Optional[DiscountOffer]:
        now = datetime.utcnow()
        result = await db.execute(
            select(DiscountOffer)
            .options(selectinload(DiscountOffer.user).selectinload(User.subscription))
            .where(
                DiscountOffer.user_id == user_id,
                DiscountOffer.notification_type.in_(self.NOTIFICATION_TYPES),
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
        member: LegacyProOfferMember,
    ) -> DiscountOffer:
        notification_type = self.notification_type_for_member(member)
        if not notification_type:
            raise ValueError(
                f"Unsupported Legacy Pro offer member group/price: {member}"
            )

        existing = await self.get_active_offer(db, user.id)
        if existing:
            return existing

        expires_at = self.offer_expires_at()
        now = datetime.utcnow()
        valid_hours = max(1, int((expires_at - now).total_seconds() // 3600))
        offer = await upsert_discount_offer(
            db,
            user_id=user.id,
            subscription_id=getattr(getattr(user, "subscription", None), "id", None),
            notification_type=notification_type,
            discount_percent=0,
            bonus_amount_kopeks=0,
            valid_hours=valid_hours,
            effect_type=self.EFFECT_TYPE,
            extra_data=self.build_extra_data(member),
        )
        offer.expires_at = expires_at
        await db.commit()
        await db.refresh(offer)
        return offer

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
        if user is not None and self.has_converted_to_tariff(user):
            return None, None
        if not self.validate_offer_for_plan(
            offer,
            plan_code=plan_code,
            period_days=period_days,
        ):
            return None, None

        extra = getattr(offer, "extra_data", None) or {}
        try:
            price = int(extra.get("price_kopeks") or 0)
        except (TypeError, ValueError):
            return None, None
        return (price, offer) if price > 0 else (None, None)

    async def mark_claimed_after_purchase(
        self, db: AsyncSession, offer: Optional[DiscountOffer]
    ) -> None:
        if not offer or getattr(offer, "claimed_at", None):
            return
        extra = getattr(offer, "extra_data", None) or {}
        await db.execute(
            update(User)
            .where(User.id == offer.user_id)
            .values(tariff_pricing_cohort_override="new")
        )
        await mark_offer_claimed(
            db,
            offer,
            details={
                "context": "legacy_pro_fixed_price_purchase",
                "plan_code": self.PLAN_CODE,
                "period_days": self.PERIOD_DAYS,
                "price_kopeks": extra.get("price_kopeks"),
                "csv_group": extra.get("csv_group"),
            },
        )

    def has_converted_to_tariff(self, user: User) -> bool:
        subscription = getattr(user, "subscription", None)
        return bool(subscription and getattr(subscription, "plan_id", None) is not None)

    def is_eligible_user(self, user: User, member: LegacyProOfferMember) -> bool:
        if not user or not getattr(user, "telegram_id", None):
            return False
        if int(user.telegram_id) != member.telegram_id:
            return False
        if getattr(user, "status", None) != UserStatus.ACTIVE.value:
            return False
        if self.has_converted_to_tariff(user):
            return False
        subscription = getattr(user, "subscription", None)
        if subscription is None:
            return False
        return getattr(subscription, "plan_id", None) is None

    async def was_slot_sent(
        self, db: AsyncSession, user_id: int, slot_key: str
    ) -> bool:
        from app.database.models import InteractiveNotificationLog

        result = await db.execute(
            select(InteractiveNotificationLog.id)
            .where(
                InteractiveNotificationLog.user_id == user_id,
                InteractiveNotificationLog.slot_key == slot_key,
                InteractiveNotificationLog.status == "sent",
            )
            .limit(1)
        )
        return result.scalar_one_or_none() is not None

    def _member_from_row(self, row: dict[str, str]) -> Optional[LegacyProOfferMember]:
        try:
            telegram_id = int(row.get("telegram_id") or 0)
            if telegram_id <= 0:
                return None
            offer_year_pro_rub = int(float(row.get("offer_year_pro_rub") or 0))
            offer_per_month = int(float(row.get("offer_per_month") or 0))
            payments = int(float(row.get("payments") or 0))
            total_rub = float(row.get("total_rub") or 0)
            last_monthly_raw = row.get("last_monthly_price")
            last_monthly_price = float(last_monthly_raw) if last_monthly_raw else None
        except (TypeError, ValueError):
            return None

        return LegacyProOfferMember(
            telegram_id=telegram_id,
            username=row.get("username") or None,
            first_name=row.get("first_name") or None,
            group=row.get("group") or "",
            payments=payments,
            total_rub=total_rub,
            last_monthly_price=last_monthly_price,
            last_payment=row.get("last_payment") or None,
            offer_year_pro_rub=offer_year_pro_rub,
            offer_per_month=offer_per_month,
        )


legacy_pro_offer_service = LegacyProOfferService()
