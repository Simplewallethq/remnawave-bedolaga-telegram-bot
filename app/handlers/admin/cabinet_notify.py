"""Ручная отправка cabinet-уведомления конкретному пользователю из тг-админки.

Уведомление попадает в ленту личного кабинета и приложений teleVpn
(тип "broadcast" — фронты уже умеют его рендерить). Телеграм-сообщение
пользователю НЕ отправляется — только запись в ленту.
"""

from __future__ import annotations

import logging

from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.user import (
    get_user_by_email,
    get_user_by_telegram_id,
    get_user_by_username,
)
from app.database.models import User
from app.services.cabinet_notification_service import (
    CabinetNotificationType,
    notify,
)
from app.utils.decorators import admin_required, error_handler

logger = logging.getLogger(__name__)

MAX_TITLE_LENGTH = 255
MAX_BODY_LENGTH = 4000


class CabinetNotifyStates(StatesGroup):
    waiting_for_target = State()
    waiting_for_title = State()
    waiting_for_body = State()
    confirming = State()


def _cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cabinet_notify_cancel")]
    ])


def _confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Отправить", callback_data="cabinet_notify_confirm")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cabinet_notify_cancel")],
    ])


def _back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📬 Отправить ещё", callback_data="admin_cabinet_notify")],
        [InlineKeyboardButton(text="🔙 К рассылкам", callback_data="admin_messages")],
    ])


def _describe_user(user: User) -> str:
    parts = [f"<b>{user.full_name}</b> (id {user.id})"]
    if user.telegram_id:
        parts.append(f"Telegram ID: <code>{user.telegram_id}</code>")
    if user.username:
        parts.append(f"Username: @{user.username}")
    if getattr(user, "email", None):
        parts.append(f"Email: {user.email}")
    return "\n".join(parts)


async def _resolve_user(db: AsyncSession, query: str) -> User | None:
    """Telegram ID (цифры), @username или email — как в admin/users поиске."""
    normalized = query.strip()
    if normalized.startswith("@"):
        return await get_user_by_username(db, normalized[1:])
    if normalized.isdigit():
        return await get_user_by_telegram_id(db, int(normalized))
    if "@" in normalized:
        return await get_user_by_email(db, normalized)
    return await get_user_by_username(db, normalized)


@admin_required
@error_handler
async def start_cabinet_notify(
    callback: types.CallbackQuery,
    state: FSMContext,
    db_user: User,
    db: AsyncSession,
):
    if not (settings.CABINET_ENABLED and settings.CABINET_NOTIFICATIONS_ENABLED):
        await callback.answer(
            "❌ Уведомления кабинета выключены (CABINET_NOTIFICATIONS_ENABLED).",
            show_alert=True,
        )
        return

    await state.clear()
    await callback.message.edit_text(
        "📬 <b>Уведомление в кабинет</b>\n\n"
        "Уведомление появится в ленте личного кабинета и приложений "
        "(телеграм-сообщение не отправляется).\n\n"
        "Введите получателя: <b>Telegram ID</b>, <b>@username</b> или <b>email</b>.\n\n"
        "Отправьте /cancel для отмены.",
        reply_markup=_cancel_keyboard(),
        parse_mode="HTML",
    )
    await state.set_state(CabinetNotifyStates.waiting_for_target)
    await callback.answer()


@admin_required
@error_handler
async def process_target(
    message: types.Message,
    state: FSMContext,
    db_user: User,
    db: AsyncSession,
):
    if message.text == "/cancel":
        await _cancel_to_menu_message(message, state)
        return

    user = await _resolve_user(db, message.text or "")
    if not user:
        await message.answer(
            "❌ Пользователь не найден. Введите Telegram ID, @username или email, "
            "либо /cancel для отмены.",
            reply_markup=_cancel_keyboard(),
        )
        return

    await state.update_data(target_user_id=user.id, target_label=_describe_user(user))
    await message.answer(
        f"👤 Получатель:\n{_describe_user(user)}\n\n"
        f"Введите <b>заголовок</b> уведомления "
        f"или /skip для стандартного («Объявление»).",
        reply_markup=_cancel_keyboard(),
        parse_mode="HTML",
    )
    await state.set_state(CabinetNotifyStates.waiting_for_title)


@admin_required
@error_handler
async def process_title(
    message: types.Message,
    state: FSMContext,
    db_user: User,
    db: AsyncSession,
):
    if message.text == "/cancel":
        await _cancel_to_menu_message(message, state)
        return

    title = None
    if message.text != "/skip":
        title = message.text.strip()
        if len(title) > MAX_TITLE_LENGTH:
            await message.answer(
                f"❌ Заголовок слишком длинный (максимум {MAX_TITLE_LENGTH} символов). "
                f"Попробуйте ещё раз или /skip.",
                reply_markup=_cancel_keyboard(),
            )
            return

    await state.update_data(title=title)
    await message.answer(
        "Введите <b>текст</b> уведомления.",
        reply_markup=_cancel_keyboard(),
        parse_mode="HTML",
    )
    await state.set_state(CabinetNotifyStates.waiting_for_body)


@admin_required
@error_handler
async def process_body(
    message: types.Message,
    state: FSMContext,
    db_user: User,
    db: AsyncSession,
):
    if message.text == "/cancel":
        await _cancel_to_menu_message(message, state)
        return

    body = (message.text or "").strip()
    if not body:
        await message.answer(
            "❌ Текст пустой. Введите текст уведомления или /cancel.",
            reply_markup=_cancel_keyboard(),
        )
        return
    if len(body) > MAX_BODY_LENGTH:
        await message.answer(
            f"❌ Текст слишком длинный (максимум {MAX_BODY_LENGTH} символов). "
            f"Попробуйте ещё раз.",
            reply_markup=_cancel_keyboard(),
        )
        return

    await state.update_data(body=body)
    data = await state.get_data()
    title = data.get("title") or "Объявление"

    await message.answer(
        f"📬 <b>Проверьте уведомление</b>\n\n"
        f"👤 Получатель:\n{data['target_label']}\n\n"
        f"<b>{title}</b>\n"
        f"<blockquote>{body}</blockquote>",
        reply_markup=_confirm_keyboard(),
        parse_mode="HTML",
    )
    await state.set_state(CabinetNotifyStates.confirming)


@admin_required
@error_handler
async def confirm_cabinet_notify(
    callback: types.CallbackQuery,
    state: FSMContext,
    db_user: User,
    db: AsyncSession,
):
    data = await state.get_data()
    target_user_id = data.get("target_user_id")
    body = data.get("body")
    if not target_user_id or not body:
        await state.clear()
        await callback.answer("❌ Данные утеряны, начните заново.", show_alert=True)
        return

    notification = await notify(
        db,
        user_id=target_user_id,
        type=CabinetNotificationType.BROADCAST,
        title=data.get("title"),
        body=body,
        payload={"manual": True, "adminId": db_user.id},
    )
    await state.clear()

    if notification is None:
        logger.error(
            "Ручное уведомление кабинета для пользователя %s не создано",
            target_user_id,
        )
        await callback.message.edit_text(
            "❌ Не удалось создать уведомление (подробности в логах).",
            reply_markup=_back_keyboard(),
        )
        await callback.answer()
        return

    logger.info(
        "Админ %s отправил ручное уведомление кабинета #%s пользователю %s",
        db_user.id,
        notification.id,
        target_user_id,
    )
    await callback.message.edit_text(
        f"✅ <b>Уведомление отправлено</b> (id {notification.id})\n\n"
        f"👤 Получатель:\n{data['target_label']}",
        reply_markup=_back_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


@admin_required
@error_handler
async def cancel_cabinet_notify(
    callback: types.CallbackQuery,
    state: FSMContext,
    db_user: User,
    db: AsyncSession,
):
    await state.clear()
    await callback.message.edit_text(
        "❌ Отправка уведомления отменена.",
        reply_markup=_back_keyboard(),
    )
    await callback.answer()


async def _cancel_to_menu_message(message: types.Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "❌ Отправка уведомления отменена.",
        reply_markup=_back_keyboard(),
    )


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(start_cabinet_notify, F.data == "admin_cabinet_notify")
    dp.callback_query.register(confirm_cabinet_notify, F.data == "cabinet_notify_confirm")
    dp.callback_query.register(cancel_cabinet_notify, F.data == "cabinet_notify_cancel")
    dp.message.register(process_target, CabinetNotifyStates.waiting_for_target)
    dp.message.register(process_title, CabinetNotifyStates.waiting_for_title)
    dp.message.register(process_body, CabinetNotifyStates.waiting_for_body)
