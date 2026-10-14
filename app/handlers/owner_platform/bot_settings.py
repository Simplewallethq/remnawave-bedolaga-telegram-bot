"""Платформа владельцев: настройки своих ботов — саппорт, цены, картинка, удаление."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from aiogram import types
from aiogram.fsm.context import FSMContext
from aiogram.types import FSInputFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.branding.context import current_brand
from app.config import settings
from app.database.models import CopycatBot, User
from app.keyboards.owner import (
    back_keyboard,
    bot_keyboard,
    delete_confirm_keyboard,
    delete_keyboard,
    picture_keyboard,
    prices_keyboard,
    settings_keyboard,
    support_keyboard,
)
from app.services import owner_platform_service as platform
from app.states import OwnerStates

from .common import answer_screen, language_of, show_home, t
from .create import picture_file, reply_not_a_picture

logger = logging.getLogger(__name__)

_DEVICES = {"solo": "1 устройство", "plus": "3 устройства", "pro": "10 устройств"}


def _row_id(callback: types.CallbackQuery) -> int:
    return int(callback.data.rsplit(":", 1)[-1])


async def _owned_bot(callback: types.CallbackQuery, db: AsyncSession, db_user: User) -> Optional[CopycatBot]:
    row = await platform.get_owner_bot(db, db_user.id, _row_id(callback))
    if row is None or not row.enabled:
        await callback.answer(t(language_of(db_user), "OWNER_BOT_NOT_FOUND", "Бот не найден"), show_alert=True)
        return None
    return row


# ---------------------------------------------------------------- список и бот


async def show_settings(callback: types.CallbackQuery, state: FSMContext, db: AsyncSession, db_user: User):
    await state.clear()
    await callback.answer()
    language = language_of(db_user)
    bots = await platform.list_owner_bots(db, db_user.id, enabled_only=True)
    text = t(
        language,
        "OWNER_SETTINGS_TEXT",
        "Здесь ты можешь поменять настройки своих ботов, создать новых и удалить текущие.",
    )
    await answer_screen(callback, text, settings_keyboard(language, [(row.id, row.username or "?") for row in bots]))


async def _render_bot(target, row: CopycatBot, language: str, *, edit: bool = True) -> None:
    working = await platform.bot_is_working(row)
    status = (
        t(language, "OWNER_STATUS_OK", "🟢 работает")
        if working
        else t(language, "OWNER_STATUS_DOWN", "🔴 не работает")
    )
    text = t(
        language,
        "OWNER_BOT_TEXT",
        "Текущий бот: @{bot}\nСтатус: {status}",
        bot=row.username,
        status=status,
    )
    if not working:
        text += "\n\n" + t(
            language,
            "OWNER_STATUS_DOWN_HINT",
            "Токен бота больше не действует. Удали бота и подключи заново с новым токеном из @BotFather — "
            "подписчики и настройки сохранятся.",
        )
    text += "\n\n" + t(language, "OWNER_BOT_HINT", "Ты можешь настроить его с помощью пунктов меню ниже.")
    await answer_screen(target, text, bot_keyboard(language, row.id), edit=edit)


async def show_bot(callback: types.CallbackQuery, state: FSMContext, db: AsyncSession, db_user: User):
    row = await _owned_bot(callback, db, db_user)
    if row is None:
        return
    await state.clear()
    await callback.answer()
    await _render_bot(callback, row, language_of(db_user))


# ---------------------------------------------------------------- саппорт


async def _render_support(target, row: CopycatBot, language: str, *, edit: bool = True) -> None:
    support = (row.support or "").strip() or "—"
    if support != "—" and not support.startswith("@") and "/" not in support:
        support = f"@{support}"
    text = t(
        language,
        "OWNER_SUPPORT_TEXT",
        "Текущий саппорт юзернейм: {support}. Для того чтобы сменить, нажми на кнопку ниже.",
        support=support,
    )
    await answer_screen(target, text, support_keyboard(language, row.id), edit=edit)


async def show_support(callback: types.CallbackQuery, state: FSMContext, db: AsyncSession, db_user: User):
    row = await _owned_bot(callback, db, db_user)
    if row is None:
        return
    await state.clear()
    await callback.answer()
    await _render_support(callback, row, language_of(db_user))


async def edit_support(callback: types.CallbackQuery, state: FSMContext, db: AsyncSession, db_user: User):
    row = await _owned_bot(callback, db, db_user)
    if row is None:
        return
    await callback.answer()
    language = language_of(db_user)
    await state.set_state(OwnerStates.waiting_for_new_support)
    await state.update_data(wl_row=row.id)
    await answer_screen(
        callback,
        t(language, "OWNER_SUPPORT_PROMPT", "Введи юзернейм для поддержки с символом «@» или без него."),
        back_keyboard(language, f"wl_bot:{row.id}"),
    )


async def handle_new_support(message: types.Message, state: FSMContext, db: AsyncSession, db_user: User):
    language = language_of(db_user)
    row = await platform.get_owner_bot(db, db_user.id, int((await state.get_data()).get("wl_row") or 0))
    if row is None or not row.enabled:
        await state.clear()
        await show_home(message, db, db_user)
        return
    support = platform.normalize_support_username(message.text or "")
    if support is None:
        await message.answer(
            t(language, "OWNER_SUPPORT_INVALID", "Юзернейм может содержать латиницу, цифры и _ (5–32 символа)."),
            reply_markup=back_keyboard(language, f"wl_bot:{row.id}"),
        )
        return
    row.support = f"@{support}"
    await db.commit()
    await state.clear()
    await platform.refresh_runtime()
    await _render_support(message, row, language)


# ---------------------------------------------------------------- цены


def _prices_text(language: str, username: str, prices: dict[str, int]) -> str:
    lines = [t(language, "OWNER_PRICES_TITLE", "Текущие цены для @{bot}", bot=username), ""]
    for code, title in platform.PLAN_TITLES.items():
        lines.append(
            t(language, "OWNER_PRICES_LINE", "<b>{plan} – {price}₽ в мес</b>", plan=title, price=prices.get(code, 0))
        )
        lines.append(
            t(
                language,
                f"OWNER_PRICES_{code.upper()}_FEATURES",
                "{devices} / все сервисы / анлим траффик",
                devices=_DEVICES[code],
            )
        )
        lines.append("")
    return "\n".join(lines).rstrip()


async def _render_prices(target, row: CopycatBot, language: str, *, edit: bool = True) -> None:
    text = _prices_text(language, row.username or "?", platform.current_monthly_prices(row))
    await answer_screen(target, text, prices_keyboard(language, row.id), edit=edit)


async def show_prices(callback: types.CallbackQuery, state: FSMContext, db: AsyncSession, db_user: User):
    row = await _owned_bot(callback, db, db_user)
    if row is None:
        return
    await state.clear()
    await callback.answer()
    await _render_prices(callback, row, language_of(db_user))


_PRICE_STEPS = (
    ("solo", OwnerStates.waiting_for_solo_price, "OWNER_PRICE_SOLO_PROMPT"),
    ("plus", OwnerStates.waiting_for_plus_price, "OWNER_PRICE_PLUS_PROMPT"),
    ("pro", OwnerStates.waiting_for_pro_price, "OWNER_PRICE_PRO_PROMPT"),
)
_PRICE_PROMPT_DEFAULT = (
    "Текущая цена {plan} – {price}₽ в мес. Введи новую цену тарифа {plan} в рублях за месяц "
    "(целое число), или ту же текущую, чтобы не менять."
)


async def _prompt_price(target, language: str, row: CopycatBot, step: int, *, edit: bool = False) -> None:
    code, _, key = _PRICE_STEPS[step]
    current = platform.current_monthly_prices(row).get(code, 0)
    text = t(language, key, _PRICE_PROMPT_DEFAULT, plan=platform.PLAN_TITLES[code], price=current)
    await answer_screen(target, text, back_keyboard(language, f"wl_bot:{row.id}"), edit=edit)


async def edit_prices(callback: types.CallbackQuery, state: FSMContext, db: AsyncSession, db_user: User):
    row = await _owned_bot(callback, db, db_user)
    if row is None:
        return
    await callback.answer()
    await state.set_state(_PRICE_STEPS[0][1])
    await state.set_data({"wl_row": row.id})
    await _prompt_price(callback, language_of(db_user), row, 0, edit=True)


async def handle_price(message: types.Message, state: FSMContext, db: AsyncSession, db_user: User):
    language = language_of(db_user)
    data = await state.get_data()
    row = await platform.get_owner_bot(db, db_user.id, int(data.get("wl_row") or 0))
    if row is None or not row.enabled:
        await state.clear()
        await show_home(message, db, db_user)
        return

    current_state = await state.get_state()
    step = next(index for index, item in enumerate(_PRICE_STEPS) if item[1].state == current_state)
    code = _PRICE_STEPS[step][0]
    back = back_keyboard(language, f"wl_bot:{row.id}")

    value = platform.parse_price(message.text or "")
    if value is None:
        await message.answer(t(language, "OWNER_PRICE_NOT_INT", "Введи целое число, например 199."), reply_markup=back)
        return
    error = platform.price_error(code, value, solo=data.get("wl_solo"), plus=data.get("wl_plus"))
    if error is not None:
        key, values = error
        defaults = {
            "OWNER_PRICE_SOLO_MIN": "Минимальная цена на Solo {min}₽.",
            "OWNER_PRICE_PLUS_STEP": "Цена на Plus должна быть выше цены Solo на 10%, то есть минимум {min}₽.",
            "OWNER_PRICE_PRO_STEP": "Цена на Pro должна быть выше цены Plus на 20%, то есть минимум {min}₽.",
            "OWNER_PRICE_FLOOR": "Минимальная цена на {plan} — {min}₽.",
        }
        await message.answer(t(language, key, defaults[key], **values), reply_markup=back)
        return

    await state.update_data(**{f"wl_{code}": value})
    if step + 1 < len(_PRICE_STEPS):
        await state.set_state(_PRICE_STEPS[step + 1][1])
        await _prompt_price(message, language, row, step + 1)
        return

    data = await state.get_data()
    await state.clear()
    await platform.save_owner_prices(db, row, {code: int(data[f"wl_{code}"]) for code, _, _ in _PRICE_STEPS})
    await _render_prices(message, row, language)


# ---------------------------------------------------------------- картинка


async def _render_picture(target, row: CopycatBot, language: str) -> None:
    text = t(
        language,
        "OWNER_PICTURE_TEXT",
        "Здесь ты можешь сменить текущую картинку для @{bot}",
        bot=row.username,
    )
    keyboard = picture_keyboard(language, row.id)
    message = target.message if isinstance(target, types.CallbackQuery) else target
    logo = Path(settings.resolve_bot_logo(row.logo or settings.LOGO_FILE))
    if logo.exists():
        try:
            await message.answer_photo(FSInputFile(logo), caption=text, reply_markup=keyboard)
            if isinstance(target, types.CallbackQuery):
                try:
                    await target.message.delete()
                except Exception:  # noqa: BLE001
                    pass
            return
        except Exception as error:  # noqa: BLE001
            logger.warning("Не удалось показать картинку бота владельца: %s", error)
    await answer_screen(target, text, keyboard)


async def show_picture(callback: types.CallbackQuery, state: FSMContext, db: AsyncSession, db_user: User):
    row = await _owned_bot(callback, db, db_user)
    if row is None:
        return
    await state.clear()
    await callback.answer()
    await _render_picture(callback, row, language_of(db_user))


async def edit_picture(callback: types.CallbackQuery, state: FSMContext, db: AsyncSession, db_user: User):
    row = await _owned_bot(callback, db, db_user)
    if row is None:
        return
    await callback.answer()
    language = language_of(db_user)
    await state.set_state(OwnerStates.waiting_for_new_picture)
    await state.set_data({"wl_row": row.id})
    await answer_screen(
        callback,
        t(language, "OWNER_PICTURE_PROMPT", "Закачай новую картинку для @{bot}", bot=row.username),
        back_keyboard(language, f"wl_bot:{row.id}"),
    )


async def handle_new_picture(message: types.Message, state: FSMContext, db: AsyncSession, db_user: User):
    language = language_of(db_user)
    row = await platform.get_owner_bot(db, db_user.id, int((await state.get_data()).get("wl_row") or 0))
    if row is None or not row.enabled:
        await state.clear()
        await show_home(message, db, db_user)
        return
    picture = picture_file(message)
    if picture is None:
        await reply_not_a_picture(message, language, f"wl_pic_edit:{row.id}")
        return
    file_id, suffix = picture
    try:
        row.logo = await platform.save_picture(message.bot, file_id, row.telegram_bot_id or row.id, suffix=suffix)
    except Exception as error:  # noqa: BLE001
        logger.error("Не удалось сохранить новую картинку бота владельца: %s", error)
        await reply_not_a_picture(message, language, f"wl_pic_edit:{row.id}")
        return
    await db.commit()
    await state.clear()
    await platform.refresh_runtime()
    await _render_picture(message, row, language)


# ---------------------------------------------------------------- удаление


async def ask_delete(callback: types.CallbackQuery, state: FSMContext, db: AsyncSession, db_user: User):
    row = await _owned_bot(callback, db, db_user)
    if row is None:
        return
    await state.clear()
    await callback.answer()
    language = language_of(db_user)
    text = t(
        language,
        "OWNER_DELETE_TEXT",
        "Ты можешь удалить бота @{bot} с платформы {platform}. Подписчики и настройки сохранятся — "
        "если подключишь бота снова, всё вернётся.",
        bot=row.username,
        platform=current_brand().name,
    )
    await answer_screen(callback, text, delete_keyboard(language, row.id, row.username or "?"))


async def confirm_delete(callback: types.CallbackQuery, state: FSMContext, db: AsyncSession, db_user: User):
    row = await _owned_bot(callback, db, db_user)
    if row is None:
        return
    await callback.answer()
    language = language_of(db_user)
    await answer_screen(
        callback,
        t(language, "OWNER_DELETE_CONFIRM_TEXT", "Ты точно хочешь удалить @{bot}?", bot=row.username),
        delete_confirm_keyboard(language, row.id),
    )


async def do_delete(callback: types.CallbackQuery, state: FSMContext, db: AsyncSession, db_user: User):
    row = await _owned_bot(callback, db, db_user)
    if row is None:
        return
    await platform.disable_bot(db, row)
    await platform.refresh_runtime()
    await callback.answer(
        t(language_of(db_user), "OWNER_DELETED", "Бот @{bot} удалён", bot=row.username)
    )
    await show_home(callback, db, db_user)
