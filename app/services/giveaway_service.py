"""Розыгрыш с билетами: условия, подсчёт билетов и снимок участника.

Каждое условие считается заново при открытии экрана и при каждом «Проверить»:
- подписка на канал — через Telegram API (бот должен быть админом канала);
- друзья — пользователи, пришедшие по реф-ссылке внутри окна розыгрыша;
- оплатившие друзья — из них те, у кого в окне есть оплата подписки от порога;
- тариф — активная платная подписка на момент проверки (триал не считается).

Билеты складываются по всем выполненным строкам: 9 друзей дают 2 + 4 + 6.
Результат последней проверки пишется в giveaway_entries — это список участников
для итогов.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import (
    GiveawayEntry,
    PaymentMethod,
    Subscription,
    Transaction,
    TransactionType,
    User,
    UserStatus,
)

logger = logging.getLogger(__name__)

CHANNEL_TICKETS = 1
# Порог друзей → билеты. Строки независимые и суммируются.
INVITE_TIERS: tuple[tuple[int, int], ...] = ((3, 2), (6, 4), (9, 6))
PLAN_TICKETS: dict[str, int] = {"solo": 2, "plus": 3, "pro": 4}
PLAN_TITLES: dict[str, str] = {"solo": "Solo", "plus": "Plus", "pro": "Pro"}
FRIEND_PAID_TICKETS = 4
# Подписка без тарифа (до перехода на планы) считается как Solo.
LEGACY_PLAN_CODE = "solo"

_GOOD_MEMBER_STATUSES = (
    ChatMemberStatus.MEMBER,
    ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.CREATOR,
)


@dataclass(frozen=True)
class GiveawayProgress:
    # None — Telegram не ответил (бот не админ канала, сеть): условие не засчитано.
    channel_subscribed: Optional[bool]
    invited_count: int
    invited_paid_count: int
    plan_code: Optional[str]

    @property
    def channel_tickets(self) -> int:
        return CHANNEL_TICKETS if self.channel_subscribed else 0

    def invite_tier_reached(self, threshold: int) -> bool:
        return self.invited_count >= threshold

    @property
    def invite_tickets(self) -> int:
        return sum(
            tickets for threshold, tickets in INVITE_TIERS
            if self.invite_tier_reached(threshold)
        )

    @property
    def plan_tickets(self) -> int:
        return PLAN_TICKETS.get(self.plan_code or "", 0)

    @property
    def friend_paid_tickets(self) -> int:
        return FRIEND_PAID_TICKETS * self.invited_paid_count

    @property
    def total_tickets(self) -> int:
        return (
            self.channel_tickets
            + self.invite_tickets
            + self.plan_tickets
            + self.friend_paid_tickets
        )


@dataclass(frozen=True)
class GiveawaySummary:
    participants: int
    tickets: int
    channel_subscribed: int
    invited: int
    invited_paid: int
    plans: dict[str, int]


def _parse_moment(value: str) -> datetime:
    """ISO-дата из конфига → naive UTC, как хранятся даты в базе."""
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        return moment
    return moment.astimezone(timezone.utc).replace(tzinfo=None)


class GiveawayService:
    @property
    def code(self) -> str:
        return settings.GIVEAWAY_CODE

    @property
    def starts_at(self) -> datetime:
        return _parse_moment(settings.GIVEAWAY_START_AT)

    @property
    def ends_at(self) -> datetime:
        return _parse_moment(settings.GIVEAWAY_END_AT)

    def is_running(self, now_utc: Optional[datetime] = None) -> bool:
        if not settings.GIVEAWAY_ENABLED:
            return False
        now = now_utc or datetime.utcnow()
        return self.starts_at <= now <= self.ends_at

    def _friends_filter(self, user_id: int):
        return and_(
            User.referred_by_id == user_id,
            User.status != UserStatus.DELETED.value,
            User.created_at >= self.starts_at,
            User.created_at <= self.ends_at,
        )

    async def count_invited(self, db: AsyncSession, user_id: int) -> int:
        result = await db.execute(
            select(func.count(User.id)).where(self._friends_filter(user_id))
        )
        return int(result.scalar() or 0)

    async def count_invited_paid(self, db: AsyncSession, user_id: int) -> int:
        result = await db.execute(
            select(func.count(func.distinct(User.id)))
            .select_from(User)
            .join(Transaction, Transaction.user_id == User.id)
            .where(
                self._friends_filter(user_id),
                Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
                Transaction.is_completed.is_(True),
                or_(
                    Transaction.amount_kopeks >= settings.GIVEAWAY_MIN_FRIEND_PAYMENT_KOPEKS,
                    # Покупка в iOS пишется с суммой 0 — деньги получает Apple.
                    Transaction.payment_method == PaymentMethod.APPLE_IAP.value,
                ),
                Transaction.created_at >= self.starts_at,
                Transaction.created_at <= self.ends_at,
            )
        )
        return int(result.scalar() or 0)

    async def active_plan_code(self, db: AsyncSession, user_id: int) -> Optional[str]:
        subscription = (
            await db.execute(select(Subscription).where(Subscription.user_id == user_id))
        ).unique().scalar_one_or_none()
        if (
            subscription is None
            or not subscription.is_active
            or subscription.is_trial
            or subscription.is_paid_trial
        ):
            return None
        if subscription.plan is None:
            return LEGACY_PLAN_CODE
        code = (subscription.plan.code or "").lower()
        return code if code in PLAN_TICKETS else None

    async def check_channel(self, bot: Bot, telegram_id: Optional[int]) -> Optional[bool]:
        if not telegram_id:
            return False
        try:
            member = await bot.get_chat_member(
                chat_id=settings.GIVEAWAY_CHANNEL, user_id=telegram_id
            )
        except Exception as exc:  # noqa: BLE001 — любая ошибка API = «не проверено»
            logger.warning(
                "Розыгрыш: не удалось проверить подписку %s на %s: %s",
                telegram_id,
                settings.GIVEAWAY_CHANNEL,
                exc,
            )
            return None
        if member.status in _GOOD_MEMBER_STATUSES:
            return True
        # Ограниченный участник всё ещё в канале.
        return member.status == ChatMemberStatus.RESTRICTED and bool(
            getattr(member, "is_member", False)
        )

    async def collect(self, db: AsyncSession, bot: Bot, user: User) -> GiveawayProgress:
        invited = await self.count_invited(db, user.id)
        return GiveawayProgress(
            channel_subscribed=await self.check_channel(bot, user.telegram_id),
            invited_count=invited,
            # У transactions нет индекса по user_id: без новых друзей джойн не нужен.
            invited_paid_count=await self.count_invited_paid(db, user.id) if invited else 0,
            plan_code=await self.active_plan_code(db, user.id),
        )

    async def get_entry(self, db: AsyncSession, user_id: int) -> Optional[GiveawayEntry]:
        result = await db.execute(
            select(GiveawayEntry).where(
                GiveawayEntry.giveaway_code == self.code,
                GiveawayEntry.user_id == user_id,
            )
        )
        return result.scalar_one_or_none()

    async def summary(self, db: AsyncSession) -> GiveawaySummary:
        entry = GiveawayEntry
        row = (
            await db.execute(
                select(
                    func.count(entry.id),
                    func.coalesce(func.sum(entry.tickets), 0),
                    func.count(entry.id).filter(entry.channel_subscribed.is_(True)),
                    func.coalesce(func.sum(entry.invited_count), 0),
                    func.coalesce(func.sum(entry.invited_paid_count), 0),
                ).where(entry.giveaway_code == self.code)
            )
        ).one()
        plans = dict(
            (
                await db.execute(
                    select(entry.plan_code, func.count(entry.id))
                    .where(entry.giveaway_code == self.code, entry.plan_code.is_not(None))
                    .group_by(entry.plan_code)
                )
            ).all()
        )
        return GiveawaySummary(
            participants=int(row[0]),
            tickets=int(row[1]),
            channel_subscribed=int(row[2]),
            invited=int(row[3]),
            invited_paid=int(row[4]),
            plans={code: int(plans.get(code, 0)) for code in PLAN_TICKETS},
        )

    async def ranked_entries(
        self, db: AsyncSession, limit: Optional[int] = None
    ) -> list[tuple[GiveawayEntry, User]]:
        """Участники по убыванию билетов; при равенстве — кто раньше проверился."""
        query = (
            select(GiveawayEntry, User)
            .join(User, User.id == GiveawayEntry.user_id)
            .where(GiveawayEntry.giveaway_code == self.code)
            .order_by(GiveawayEntry.tickets.desc(), GiveawayEntry.checked_at.asc())
        )
        if limit is not None:
            query = query.limit(limit)
        return [(entry, user) for entry, user in (await db.execute(query)).all()]

    async def save_entry(
        self, db: AsyncSession, user_id: int, progress: GiveawayProgress
    ) -> None:
        values = dict(
            channel_subscribed=bool(progress.channel_subscribed),
            invited_count=progress.invited_count,
            invited_paid_count=progress.invited_paid_count,
            plan_code=progress.plan_code,
            tickets=progress.total_tickets,
            checked_at=datetime.utcnow(),
        )
        entry = await self.get_entry(db, user_id)
        if entry is None:
            db.add(GiveawayEntry(giveaway_code=self.code, user_id=user_id, **values))
        else:
            for key, value in values.items():
                setattr(entry, key, value)
        try:
            await db.commit()
        except IntegrityError:
            # Двойное нажатие: строку уже вставил параллельный апдейт — обновляем её.
            await db.rollback()
            entry = await self.get_entry(db, user_id)
            if entry is None:
                raise
            for key, value in values.items():
                setattr(entry, key, value)
            await db.commit()


giveaway_service = GiveawayService()
