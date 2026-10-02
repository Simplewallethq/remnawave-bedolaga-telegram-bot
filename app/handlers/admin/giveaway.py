"""Админка розыгрыша: сколько участников, их билеты и выгрузка списка в CSV.

Данные — снимок последней проверки каждого участника (giveaway_entries):
участник попадает в список, когда открыл экран розыгрыша или нажал «Проверить».
"""

import csv
import html
import io
import logging
from datetime import timedelta

from aiogram import Dispatcher, F, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.giveaway_service import PLAN_TITLES, giveaway_service
from app.utils.decorators import admin_required, error_handler

logger = logging.getLogger(__name__)

TOP_LIMIT = 20
# Даты в базе — naive UTC; админам показываем МСК.
_MSK = timedelta(hours=3)


def _who(user) -> str:
    if user.username:
        return f"@{html.escape(user.username)}"
    name = html.escape(user.first_name or "без имени")
    if user.telegram_id:
        return f'<a href="tg://user?id={user.telegram_id}">{name}</a>'
    return f"{name} (id {user.id})"


def build_admin_giveaway_text(summary, ranked) -> str:
    status = "идёт" if giveaway_service.is_running() else "не идёт"
    ends = (giveaway_service.ends_at + _MSK).strftime("%d.%m.%Y %H:%M")
    plans = " · ".join(
        f"{PLAN_TITLES[code]} {count}" for code, count in summary.plans.items()
    )
    lines = [
        "🏆 <b>Розыгрыш 2 × GTA VI</b>",
        f"Статус: <b>{status}</b> · окончание {ends} МСК",
        "",
        f"👥 Участников: <b>{summary.participants}</b>",
        f"🎟 Билетов всего: <b>{summary.tickets}</b>",
        f"📢 Подписаны на канал: {summary.channel_subscribed}",
        f"💳 С подпиской: {plans}",
        f"🤝 Новых друзей: {summary.invited}, оплатили: {summary.invited_paid}",
    ]
    if ranked:
        lines += ["", f"<b>Топ-{len(ranked)} по билетам</b>"]
        for place, (entry, user) in enumerate(ranked, start=1):
            lines.append(f"{place}. {_who(user)} — <b>{entry.tickets}</b> 🎟")
    else:
        lines += ["", "Пока никто не участвует."]
    lines += [
        "",
        "<i>Билеты — на момент последней проверки участника. "
        "Полный список — в CSV.</i>",
    ]
    return "\n".join(lines)


def _keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔄 Обновить", callback_data="admin_giveaway"),
            InlineKeyboardButton(text="📥 CSV", callback_data="admin_giveaway_export"),
        ],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel")],
    ])


def build_participants_csv(ranked) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow([
        "место", "user_id", "telegram_id", "username", "имя", "билеты",
        "канал", "новых друзей", "друзей оплатили", "тариф", "проверено (МСК)",
    ])
    for place, (entry, user) in enumerate(ranked, start=1):
        writer.writerow([
            place,
            user.id,
            user.telegram_id or "",
            f"@{user.username}" if user.username else "",
            " ".join(filter(None, [user.first_name, user.last_name])),
            entry.tickets,
            "да" if entry.channel_subscribed else "нет",
            entry.invited_count,
            entry.invited_paid_count,
            PLAN_TITLES.get(entry.plan_code or "", ""),
            (entry.checked_at + _MSK).strftime("%d.%m.%Y %H:%M"),
        ])
    # BOM — чтобы Excel открыл кириллицу без танцев с кодировкой.
    return ("﻿" + buffer.getvalue()).encode("utf-8")


@admin_required
@error_handler
async def show_admin_giveaway(callback: types.CallbackQuery, db_user, db: AsyncSession):
    summary = await giveaway_service.summary(db)
    ranked = await giveaway_service.ranked_entries(db, limit=TOP_LIMIT)
    text = build_admin_giveaway_text(summary, ranked)
    try:
        await callback.message.edit_text(
            text, reply_markup=_keyboard(), parse_mode="HTML", disable_web_page_preview=True
        )
    except TelegramBadRequest as error:
        if "message is not modified" not in str(error):
            await callback.message.answer(
                text, reply_markup=_keyboard(), parse_mode="HTML", disable_web_page_preview=True
            )
    await callback.answer()


@admin_required
@error_handler
async def export_admin_giveaway(callback: types.CallbackQuery, db_user, db: AsyncSession):
    ranked = await giveaway_service.ranked_entries(db)
    if not ranked:
        await callback.answer("Участников пока нет", show_alert=True)
        return
    document = types.BufferedInputFile(
        build_participants_csv(ranked), filename=f"giveaway_{giveaway_service.code}.csv"
    )
    await callback.message.answer_document(
        document,
        caption=f"🏆 Участники розыгрыша: {len(ranked)}, "
        f"билетов: {sum(entry.tickets for entry, _ in ranked)}",
    )
    await callback.answer()


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_admin_giveaway, F.data == "admin_giveaway")
    dp.callback_query.register(export_admin_giveaway, F.data == "admin_giveaway_export")
