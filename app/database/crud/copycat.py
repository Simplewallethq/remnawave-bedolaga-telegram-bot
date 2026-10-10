"""Копикеты в БД: боты-витрины и их группы."""

from __future__ import annotations

from typing import Iterable, List, Optional

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models import CopycatBot, CopycatGroup


async def list_copycat_bots(
    db: AsyncSession, *, group_id: Optional[int] = None
) -> List[CopycatBot]:
    query = select(CopycatBot).options(selectinload(CopycatBot.group)).order_by(CopycatBot.id)
    if group_id is not None:
        query = query.where(CopycatBot.group_id == group_id)
    result = await db.execute(query)
    return list(result.scalars().all())


async def count_copycat_bots(db: AsyncSession) -> int:
    result = await db.execute(select(func.count(CopycatBot.id)))
    return int(result.scalar_one() or 0)


async def get_copycat_bot_by_token(db: AsyncSession, token: str) -> Optional[CopycatBot]:
    result = await db.execute(select(CopycatBot).where(CopycatBot.token == token))
    return result.scalar_one_or_none()


async def find_copycat_bot(db: AsyncSession, ref: str) -> Optional[CopycatBot]:
    """Бот по ссылке из CLI: id записи, Telegram id, @username или токен."""
    ref = (ref or "").strip()
    if not ref:
        return None
    conditions = [CopycatBot.token == ref, func.lower(CopycatBot.username) == ref.lstrip("@").lower()]
    if ref.isdigit():
        conditions.append(CopycatBot.telegram_bot_id == int(ref))
        if int(ref) < 2**31:
            conditions.append(CopycatBot.id == int(ref))
    result = await db.execute(
        select(CopycatBot).options(selectinload(CopycatBot.group)).where(or_(*conditions))
    )
    rows = list(result.scalars().all())
    if len(rows) == 1:
        return rows[0]
    # Число совпало и с id записи, и с Telegram id — Telegram id точнее.
    for row in rows:
        if ref.isdigit() and row.telegram_bot_id == int(ref):
            return row
    return rows[0] if rows else None


async def list_copycat_groups(db: AsyncSession) -> List[CopycatGroup]:
    result = await db.execute(select(CopycatGroup).order_by(CopycatGroup.id))
    return list(result.scalars().all())


async def get_copycat_group_by_name(db: AsyncSession, name: str) -> Optional[CopycatGroup]:
    result = await db.execute(
        select(CopycatGroup).where(func.lower(CopycatGroup.name) == (name or "").strip().lower())
    )
    return result.scalar_one_or_none()


async def get_or_create_copycat_group(db: AsyncSession, name: str) -> CopycatGroup:
    group = await get_copycat_group_by_name(db, name)
    if group is None:
        group = CopycatGroup(name=name.strip())
        db.add(group)
        await db.flush()
    return group


async def existing_tokens(db: AsyncSession, tokens: Iterable[str]) -> set[str]:
    tokens = [t for t in tokens if t]
    if not tokens:
        return set()
    result = await db.execute(select(CopycatBot.token).where(CopycatBot.token.in_(tokens)))
    return set(result.scalars().all())


async def group_bot_counts(db: AsyncSession) -> dict[Optional[int], int]:
    result = await db.execute(
        select(CopycatBot.group_id, func.count(CopycatBot.id)).group_by(CopycatBot.group_id)
    )
    return {group_id: int(count) for group_id, count in result.all()}

