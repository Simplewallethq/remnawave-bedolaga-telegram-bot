"""Фильтры получателей по бренду.

Воронки и офферы основного бренда (легаси-предложения, отзыв за бонус,
cold solo, hot invoice, A/B триала и т.п.) написаны про Leto и не должны
доходить до пользователей копикетов. Базовые уведомления (истечение,
триал, автоплатёж) идут всем, но в бренде получателя.
"""

from __future__ import annotations

from sqlalchemy import or_, true

from app.database.models import User
from app.utils.bot_registry import copycat_bot_ids, is_copycat_user


def not_copycat_user_clause():
    """SQL-условие «пользователь не из копикета» для запросов с User в FROM/JOIN."""
    ids = copycat_bot_ids()
    if not ids:
        return true()
    return or_(User.bot_id.is_(None), User.bot_id.notin_(list(ids)))


def is_copycat_recipient(user: object) -> bool:
    """Страховка у отправителя: получатель сидит в копикете."""
    return is_copycat_user(user)


__all__ = ["not_copycat_user_clause", "is_copycat_recipient"]
