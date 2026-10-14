"""Платформа владельцев: вход, выбор режима и главное меню владельца."""

from __future__ import annotations

import logging
from typing import Optional

from aiogram import Bot, types
from aiogram.filters import BaseFilter
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.branding.context import current_brand
from app.config import settings
from app.database.models import User
from app.keyboards.owner import owner_menu_keyboard, platform_choice_keyboard
from app.localization.texts import get_texts
from app.services import owner_platform_service as platform

logger = logging.getLogger(__name__)

DEFAULT_LANGUAGE = "ru"


class OwnerPlatformBot(BaseFilter):
    """Апдейт пришёл в бот-платформу."""

    async def __call__(self, event: types.TelegramObject, bot: Bot) -> bool:
        return settings.is_owner_platform_bot(getattr(bot, "id", None))


def language_of(user: Optional[User]) -> str:
    return getattr(user, "language", None) or DEFAULT_LANGUAGE


def t(language: str, key: str, default: str, **values) -> str:
    text = get_texts(language).t(key, default)
    return text.format(**values) if values else text


async def answer_screen(target: types.CallbackQuery | types.Message, text: str, keyboard, *, edit: bool = True) -> None:
    """Экран ответом на кнопку (правкой сообщения) или на сообщение (новым)."""
    if isinstance(target, types.CallbackQuery):
        message = target.message
        if edit and message is not None:
            try:
                await message.edit_text(text, reply_markup=keyboard, parse_mode="HTML", disable_web_page_preview=True)
                return
            except Exception as error:  # noqa: BLE001 - сообщение могло устареть
                logger.debug("Не удалось отредактировать экран платформы: %s", error)
        if message is not None:
            await message.answer(text, reply_markup=keyboard, parse_mode="HTML", disable_web_page_preview=True)
        return
    await target.answer(text, reply_markup=keyboard, parse_mode="HTML", disable_web_page_preview=True)


def choice_text(language: str) -> str:
    return t(
        language,
        "OWNER_CHOICE_TEXT",
        "Привет! Создай свой VPN бот за минуту или проверь сначала, как это будет выглядеть на нашем примере.",
    )


async def show_platform_choice(target: types.CallbackQuery | types.Message, language: str) -> None:
    await answer_screen(target, choice_text(language), platform_choice_keyboard(language))


def _legal_links(language: str) -> str:
    brand = current_brand()
    terms = (settings.OWNER_TERMS_URL or "").strip() or brand.terms_url
    privacy = (settings.OWNER_PRIVACY_URL or "").strip() or brand.privacy_url
    return t(
        language,
        "OWNER_MENU_LEGAL",
        '<a href="{terms}">Правила Сервиса</a> | <a href="{privacy}">Политика конфиденциальности</a>',
        terms=terms,
        privacy=privacy,
    )


async def show_owner_menu(target: types.CallbackQuery | types.Message, db: AsyncSession, user: User) -> None:
    language = language_of(user)
    stats = await platform.owner_stats(db, user.id)
    text = t(
        language,
        "OWNER_MENU_TEXT",
        "🤖 <b>Твои VPN боты</b>\n\n"
        "Активных ботов: <b>{active}</b>\n"
        "Пользователей всего: <b>{total}</b>\n"
        "Пользователей за вчера: <b>{yesterday}</b>\n\n"
        "{legal}",
        active=stats.active_bots,
        total=stats.users_total,
        yesterday=stats.users_yesterday,
        legal=_legal_links(language),
    )
    await answer_screen(target, text, owner_menu_keyboard(language))


async def show_home(target: types.CallbackQuery | types.Message, db: AsyncSession, user: User) -> None:
    """Главное владельца: есть боты — меню, нет — выбор «создать / попробовать»."""
    if await platform.list_owner_bots(db, user.id, enabled_only=True):
        await show_owner_menu(target, db, user)
    else:
        await show_platform_choice(target, language_of(user))


async def show_owner_home_on_start(message: types.Message, state: FSMContext, db: AsyncSession, user: User) -> bool:
    """/start зарегистрированного в боте-платформе. True — экран показан здесь,
    False — пользователь в режиме VPN, показывать обычное меню."""
    owner = await platform.get_owner(db, user.id)
    if owner is None or owner.mode != platform.MODE_OWNER:
        return False
    await state.clear()
    await show_home(message, db, user)
    return True
