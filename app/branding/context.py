"""Текущий бренд: какой бот сейчас «говорит».

В хендлерах его выставляет BrandContextMiddleware по боту апдейта. Фоновые
отправители оборачивают рендер и отправку в `use_brand_for_user(user)`.
ContextVar живёт в задаче: create_task копирует снимок, поэтому скоуп
ставится на месте отправки, а не вокруг цикла.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional

from app.branding.profile import BrandProfile

_current_bot_id: ContextVar[Optional[int]] = ContextVar("brand_bot_id", default=None)


def current_bot_id() -> Optional[int]:
    return _current_bot_id.get()


def current_brand() -> BrandProfile:
    from app.utils.bot_registry import get_brand_for_bot

    return get_brand_for_bot(_current_bot_id.get())


@contextmanager
def brand_scope(bot_id: Optional[int]) -> Iterator[None]:
    token = _current_bot_id.set(bot_id)
    try:
        yield
    finally:
        _current_bot_id.reset(token)


def use_brand_for_user(user: object):
    """Скоуп бренда получателя: бот, в котором он зарегистрирован."""
    return brand_scope(getattr(user, "bot_id", None))
