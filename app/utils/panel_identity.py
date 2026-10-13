"""Имя и описание пользователя в панели Remnawave.

Старый общий аккаунт (account_scope_bot_id IS NULL) — один на Telegram ID,
в панели `user_{telegram_id}`. Новый пользователь получает отдельный аккаунт
в каждом боте, и у одного Telegram ID может быть несколько панельных юзеров:
следующий получает `user_{telegram_id}_2`, `_3`…, а в описании — id бота.
"""

from __future__ import annotations

from typing import Any, Optional

from app.config import settings

_MAX_USERNAME = 64
_MAX_SUFFIX = 100


def is_scoped_account(user: Any) -> bool:
    return getattr(user, "account_scope_bot_id", None) is not None


def account_bot_id(user: Any) -> Optional[int]:
    """Бот аккаунта для подписи в панели; у старых общих аккаунтов — нет."""
    if not is_scoped_account(user):
        return None
    return getattr(user, "bot_id", None) or user.account_scope_bot_id


def panel_description_for_user(user: Any) -> str:
    return settings.format_remnawave_user_description(
        full_name=user.full_name,
        username=user.username,
        telegram_id=user.telegram_id,
        bot_id=account_bot_id(user),
    )


def base_panel_username(user: Any) -> str:
    if user.telegram_id:
        return settings.format_remnawave_username(
            full_name=user.full_name,
            username=user.username,
            telegram_id=user.telegram_id,
        )
    # Веб-пользователь без Telegram: детерминированный username.
    # Паддинг до 10 символов — RemnaWave требует username длиннее 5.
    return f"web_{user.id:06d}"


def _with_suffix(base: str, n: int) -> str:
    if n == 1:
        return base
    suffix = f"_{n}"
    return f"{base[:_MAX_USERNAME - len(suffix)]}{suffix}"


async def resolve_panel_username(api: Any, user: Any) -> str:
    """Свободный username для нового панельного юзера этого аккаунта.

    Старый аккаунт — базовое имя, как раньше. Аккаунт бота — базовое имя, а если
    оно занято другим панельным юзером (другой аккаунт того же Telegram ID), то
    первое свободное с итератором.
    """
    base = base_panel_username(user)
    if not is_scoped_account(user):
        return base

    own_uuid = getattr(user, "remnawave_uuid", None)
    for n in range(1, _MAX_SUFFIX + 1):
        candidate = _with_suffix(base, n)
        existing = await api.get_user_by_username(candidate)
        if existing is None or (own_uuid and existing.uuid == own_uuid):
            return candidate
    raise RuntimeError(f"Нет свободного username в панели для {base}")


async def find_panel_user(api: Any, user: Any, username: str) -> Any:
    """Существующий панельный юзер аккаунта или None.

    Старый аккаунт ищется по telegramId, затем по UUID и username. Аккаунт бота —
    только по своему UUID: по telegramId и базовому имени в панели лежит другой
    аккаунт того же человека, и его нельзя перезаписать.
    """
    if is_scoped_account(user):
        if user.remnawave_uuid:
            return await api.get_user_by_uuid(user.remnawave_uuid)
        return None

    if user.telegram_id:
        existing_users = await api.get_user_by_telegram_id(user.telegram_id)
        if existing_users:
            return existing_users[0]
    if user.remnawave_uuid:
        remnawave_user = await api.get_user_by_uuid(user.remnawave_uuid)
        if remnawave_user is not None:
            return remnawave_user
    return await api.get_user_by_username(username)
