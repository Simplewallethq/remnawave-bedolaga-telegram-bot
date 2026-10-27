"""Метрики витрин для админки: регистрации, оборот, платящие.

Пользователь витрины — `users.bot_id` = Telegram id бота, оборот — оплаченные
пополнения деньгами (те же условия, что в заработке владельцев). «Сегодня» и
«вчера» — календарные сутки в TIMEZONE, «7 дн» и «30 дн» — скользящие окна.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from sqlalchemy import case, func, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Transaction, User
from app.services.owner_platform_service import turnover_conditions
from app.utils.timezone import get_local_timezone

PERIODS = ("today", "yesterday", "d7", "d30", "total")


@dataclass
class Counts:
    today: int = 0
    yesterday: int = 0
    d7: int = 0
    d30: int = 0
    total: int = 0

    def add(self, other: "Counts") -> None:
        for name in PERIODS:
            setattr(self, name, getattr(self, name) + getattr(other, name))


@dataclass
class BotMetrics:
    users: Counts = field(default_factory=Counts)
    revenue: Counts = field(default_factory=Counts)  # копейки
    payers: int = 0
    last_registration: Optional[datetime] = None


@dataclass(frozen=True)
class Bounds:
    """Границы периодов в наивном UTC — так хранятся created_at."""

    today: datetime
    yesterday: datetime
    d7: datetime
    d30: datetime


def bounds(now: Optional[datetime] = None) -> Bounds:
    now = now or datetime.utcnow()
    local = now.replace(tzinfo=timezone.utc).astimezone(get_local_timezone())
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    today = midnight.astimezone(timezone.utc).replace(tzinfo=None)
    return Bounds(
        today=today,
        yesterday=today - timedelta(days=1),
        d7=now - timedelta(days=7),
        d30=now - timedelta(days=30),
    )


def _period_columns(column, created_at, b: Bounds) -> list:
    def since(start, end=None):
        condition = created_at >= start if end is None else (created_at >= start) & (created_at < end)
        return func.coalesce(func.sum(case((condition, column), else_=0)), 0)

    return [
        since(b.today),
        since(b.yesterday, b.today),
        since(b.d7),
        since(b.d30),
        func.coalesce(func.sum(column), 0),
    ]


async def collect(
    db: AsyncSession, bot_ids: Iterable[int], *, now: Optional[datetime] = None
) -> dict[int, BotMetrics]:
    """Метрики по Telegram id ботов; ботов без пользователей в ответе тоже нет."""
    bot_ids = sorted({int(bot_id) for bot_id in bot_ids if bot_id})
    if not bot_ids:
        return {}
    b = bounds(now)
    result: dict[int, BotMetrics] = {}

    users = await db.execute(
        select(User.bot_id, *_period_columns(literal_column("1"), User.created_at, b), func.max(User.created_at))
        .where(User.bot_id.in_(bot_ids))
        .group_by(User.bot_id)
    )
    for bot_id, *counts, last in users.all():
        metrics = result.setdefault(int(bot_id), BotMetrics())
        metrics.users = Counts(*(int(value) for value in counts))
        metrics.last_registration = last

    revenue = await db.execute(
        select(
            User.bot_id,
            *_period_columns(Transaction.amount_kopeks, Transaction.created_at, b),
            func.count(func.distinct(Transaction.user_id)),
        )
        .join(User, User.id == Transaction.user_id)
        .where(User.bot_id.in_(bot_ids), *turnover_conditions())
        .group_by(User.bot_id)
    )
    for bot_id, *sums, payers in revenue.all():
        metrics = result.setdefault(int(bot_id), BotMetrics())
        metrics.revenue = Counts(*(int(value) for value in sums))
        metrics.payers = int(payers)
    return result


def total(metrics: Iterable[BotMetrics]) -> BotMetrics:
    summary = BotMetrics()
    for item in metrics:
        summary.users.add(item.users)
        summary.revenue.add(item.revenue)
        summary.payers += item.payers
        if item.last_registration and (
            summary.last_registration is None or item.last_registration > summary.last_registration
        ):
            summary.last_registration = item.last_registration
    return summary
