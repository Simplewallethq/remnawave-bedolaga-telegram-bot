"""Хендлеры админки копикетов: сводка, список, карточка витрины, добавление.

Экран — одно сообщение бота, кнопки его редактируют. Сессия БД открывается
только на чтение/запись и закрывается до запросов в Telegram: прод рвёт
соединения, простаивающие в транзакции дольше секунды.
"""

from __future__ import annotations

import html
import logging
from typing import Any, Awaitable, Callable, Optional

from aiogram import BaseMiddleware, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)

from app.branding.profile import clean_brand_name
from app.config import settings
from app.constructor.service import find_token, refresh_runtime_in_background
from app.database.crud.copycat import find_copycat_bot, list_copycat_bots
from app.database.database import AsyncSessionLocal
from app.database.models import CopycatBot
from app.services.copycat_service import copycat_runtime
from app.utils.timezone import format_local_datetime

from . import service, stats

logger = logging.getLogger(__name__)

router = Router(name="copycat_admin")
router.message.filter(F.chat.type == ChatType.PRIVATE)

PAGE_SIZE = 10
SORTS = {
    "new": "сначала новые",
    "rev7": "оборот за 7 дн",
    "reg7": "регистрации за 7 дн",
    "rev": "оборот за всё время",
}
FILTERS = {"all": "все", "on": "включённые", "off": "выключенные"}


class AddStates(StatesGroup):
    token = State()
    picture = State()
    confirm = State()


class FindStates(StatesGroup):
    query = State()


class AccessMiddleware(BaseMiddleware):
    """Бот отвечает только COPYCAT_ADMIN_USER_IDS, остальным — «нет доступа»."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        if user is not None and user.id in settings.get_copycat_admin_user_ids():
            return await handler(event, data)
        if isinstance(event, CallbackQuery):
            await event.answer("Нет доступа", show_alert=True)
        elif isinstance(event, Message) and event.chat.type == ChatType.PRIVATE:
            logger.info("Админка копикетов: отказ в доступе %s (@%s)", user and user.id, user and user.username)
            # Не message.answer: в режиме логотипа он подменён и шлёт картинку основного бренда.
            await event.bot.send_message(event.chat.id, "Нет доступа.")
        return None


# ---------------------------------------------------------------- форматирование


def rub(kopeks: int) -> str:
    return f"{kopeks // 100:,}".replace(",", " ") + "₽"


def num(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def esc(value: Any) -> str:
    return html.escape(str(value or ""))


def status_icon(row: CopycatBot) -> str:
    if not row.enabled:
        return "⚪️"
    state = copycat_runtime.connection_state(row)
    return {"live": "🟢", "failed": "🔴"}.get(state, "🟡")


def periods_line(counts: stats.Counts, fmt: Callable[[int], str]) -> str:
    return (
        f"сегодня <b>{fmt(counts.today)}</b> · вчера <b>{fmt(counts.yesterday)}</b> · "
        f"7 дн <b>{fmt(counts.d7)}</b> · 30 дн <b>{fmt(counts.d30)}</b> · всего <b>{fmt(counts.total)}</b>"
    )


def kb(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=text, callback_data=data) for text, data in row] for row in rows if row]
    )


# Telegram id админа → id его текущего экрана: новый экран удаляет прежний,
# чтобы в чате не копились старые меню.
_screens: dict[int, int] = {}


async def show(
    event: Message | CallbackQuery,
    text: str,
    markup: Optional[InlineKeyboardMarkup] = None,
    *,
    photo: Optional[bytes] = None,
) -> None:
    """Экран в сообщении кнопки (редактированием) или новым сообщением."""
    message = event.message if isinstance(event, CallbackQuery) else None
    chat_id = message.chat.id if message is not None else event.chat.id
    if message is not None and photo is None and message.text is not None:
        try:
            await message.edit_text(text, reply_markup=markup)
            _screens[chat_id] = message.message_id
            return
        except TelegramBadRequest as error:
            if "not modified" in str(error):
                return
            # Сообщение не редактируется (старое или удалено) — шлём новое.
    bot = event.bot
    if photo is not None:
        sent = await bot.send_photo(chat_id, BufferedInputFile(photo, "picture.jpg"), caption=text, reply_markup=markup)
    else:
        sent = await bot.send_message(chat_id, text, reply_markup=markup)
    # Прежний экран и сообщение админа (команда, ссылка, картинка) — убираем.
    stale = {_screens.get(chat_id), (message or event).message_id} - {None, sent.message_id}
    _screens[chat_id] = sent.message_id
    for message_id in stale:
        try:
            await bot.delete_message(chat_id, message_id)
        except TelegramBadRequest:
            pass


# ---------------------------------------------------------------- сводка


async def summary_screen() -> tuple[str, InlineKeyboardMarkup]:
    async with AsyncSessionLocal() as db:
        rows = await list_copycat_bots(db)
        copycats = [row for row in rows if row.is_copycat]
        metrics = await stats.collect(db, [row.telegram_bot_id for row in copycats])
    enabled = [row for row in copycats if row.enabled]
    live = sum(1 for row in enabled if copycat_runtime.connection_state(row) == "live")
    mirrors = len(rows) - len(copycats)
    summary = stats.total(metrics.values())

    by_id = {row.telegram_bot_id: row for row in copycats}
    top = sorted(metrics.items(), key=lambda item: (item[1].revenue.d7, item[1].users.d7), reverse=True)[:5]
    top_lines = [
        f"{place}. @{esc(by_id[bot_id].username)} — <b>{rub(item.revenue.d7)}</b> · {num(item.users.d7)} рег"
        for place, (bot_id, item) in enumerate(top, 1)
        if item.revenue.d7 or item.users.d7
    ]
    conversion = f"{summary.payers / summary.users.total:.1%}" if summary.users.total else "—"
    text = "\n".join(
        [
            "<b>🧩 Копикеты</b>",
            f"Витрин: <b>{len(copycats)}</b> · включено {len(enabled)} · на связи {live}"
            + (f"\nКопии Лето ({mirrors}) в метриках не считаются" if mirrors else ""),
            "",
            "👥 <b>Регистрации</b>",
            periods_line(summary.users, num),
            "",
            "💳 <b>Оборот</b>",
            periods_line(summary.revenue, rub),
            f"Платящих: <b>{num(summary.payers)}</b> ({conversion} от регистраций)",
            "",
            "🏆 <b>Топ за 7 дней</b>",
            *(top_lines or ["пока пусто"]),
        ]
    )
    markup = kb(
        [("📋 Список", "l:new:all:0"), ("➕ Добавить", "add")],
        [("🔎 Найти", "find"), ("🔄 Обновить", "m")],
    )
    return text, markup


@router.message(CommandStart())
async def on_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    text, markup = await summary_screen()
    await show(message, text, markup)


@router.callback_query(F.data == "m")
async def on_summary(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    text, markup = await summary_screen()
    await show(callback, text, markup)
    await callback.answer()


# ---------------------------------------------------------------- список


def _sort_key(sort: str):
    if sort == "rev7":
        return lambda pair: (pair[1].revenue.d7, pair[1].users.d7, pair[0].id)
    if sort == "reg7":
        return lambda pair: (pair[1].users.d7, pair[1].revenue.d7, pair[0].id)
    if sort == "rev":
        return lambda pair: (pair[1].revenue.total, pair[1].users.total, pair[0].id)
    return lambda pair: pair[0].id


async def list_screen(sort: str, flt: str, page: int) -> tuple[str, InlineKeyboardMarkup]:
    sort = sort if sort in SORTS else "new"
    flt = flt if flt in FILTERS else "all"
    async with AsyncSessionLocal() as db:
        rows = await list_copycat_bots(db)
        metrics = await stats.collect(db, [row.telegram_bot_id for row in rows])
    if flt == "on":
        rows = [row for row in rows if row.enabled]
    elif flt == "off":
        rows = [row for row in rows if not row.enabled]
    pairs = [(row, metrics.get(row.telegram_bot_id) or stats.BotMetrics()) for row in rows]
    pairs.sort(key=_sort_key(sort), reverse=True)

    pages = max(1, (len(pairs) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(page, 0), pages - 1)
    chunk = pairs[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]

    buttons = []
    for row, item in chunk:
        mirror = " 🪞" if not row.is_copycat else ""
        label = (
            f"{status_icon(row)} #{row.id} @{row.username or '?'}{mirror} · "
            f"{num(item.users.d7)} рег · {rub(item.revenue.d7)}"
        )
        buttons.append([(label, f"b:{row.id}")])

    nav = []
    if page > 0:
        nav.append(("◀️", f"l:{sort}:{flt}:{page - 1}"))
    nav.append((f"{page + 1}/{pages}", f"l:{sort}:{flt}:{page}"))
    if page < pages - 1:
        nav.append(("▶️", f"l:{sort}:{flt}:{page + 1}"))
    sorts = list(SORTS)
    next_sort = sorts[(sorts.index(sort) + 1) % len(sorts)]
    filters = list(FILTERS)
    next_filter = filters[(filters.index(flt) + 1) % len(filters)]
    markup = kb(
        *buttons,
        nav,
        [(f"↕️ Сортировать: {SORTS[next_sort]}", f"l:{next_sort}:{flt}:0")],
        [(f"👁 Показать {FILTERS[next_filter]}", f"l:{sort}:{next_filter}:0")],
        [("🔎 Найти", "find"), ("➕ Добавить", "add"), ("🏠 Сводка", "m")],
    )
    text = (
        f"<b>📋 Витрины</b> — {FILTERS[flt]}, {len(pairs)} шт.\n"
        f"Порядок: {SORTS[sort]}. В кнопках — регистрации и оборот за 7 дней.\n"
        "🟢 на связи · 🟡 ждёт синхронизации · 🔴 Telegram не принял токен · ⚪️ выключена · 🪞 копия Лето"
    )
    return text, markup


@router.message(Command("list"))
async def on_list_command(message: Message, state: FSMContext) -> None:
    await state.clear()
    text, markup = await list_screen("new", "all", 0)
    await state.update_data(back="l:new:all:0")
    await show(message, text, markup)


@router.callback_query(F.data.startswith("l:"))
async def on_list(callback: CallbackQuery, state: FSMContext) -> None:
    _, sort, flt, page = (callback.data.split(":") + ["", "", "", "0"])[:4]
    await state.clear()
    await state.update_data(back=callback.data)
    text, markup = await list_screen(sort, flt, int(page) if page.isdigit() else 0)
    await show(callback, text, markup)
    await callback.answer()


# ---------------------------------------------------------------- карточка


async def card_screen(row_id: int, back: str, notice: Optional[str] = None) -> tuple[str, InlineKeyboardMarkup]:
    async with AsyncSessionLocal() as db:
        row = await find_copycat_bot(db, str(row_id))
        if row is None or row.id != row_id:
            return "Витрина не найдена.", kb([("⬅️ Назад", back)])
        metrics = (await stats.collect(db, [row.telegram_bot_id])).get(row.telegram_bot_id) or stats.BotMetrics()
        group = row.group.name if row.group else "—"

    kind = "копия Лето (зеркало)" if not row.is_copycat else f"бренд «{esc(row.name or clean_brand_name(row.title))}»"
    state = {
        "live": "на связи",
        "failed": "Telegram не принял токен",
        "pending": "ждёт синхронизации",
    }[copycat_runtime.connection_state(row)] if row.enabled else "выключена"
    conversion = f"{metrics.payers / metrics.users.total:.1%}" if metrics.users.total else "—"
    lines = [
        f"{status_icon(row)} <b>#{row.id} @{esc(row.username or '?')}</b> — {state}",
        esc(row.title or "—"),
        f"{kind} · группа {esc(group)}",
        f"Добавлена {format_local_datetime(row.created_at, '%d.%m.%Y')}"
        + (f" · владелец user:{row.owner_user_id}" if row.owner_user_id else ""),
        "",
        "👥 <b>Регистрации</b>",
        periods_line(metrics.users, num),
        "Последняя: " + (format_local_datetime(metrics.last_registration, "%d.%m %H:%M") if metrics.last_registration else "—"),
        "",
        "💳 <b>Оборот</b>",
        periods_line(metrics.revenue, rub),
        f"Платящих: <b>{num(metrics.payers)}</b> ({conversion} от регистраций)",
    ]
    if notice:
        lines += ["", notice]
    toggle = ("⏸ Выключить", f"off:{row.id}") if row.enabled else ("▶️ Включить", f"on:{row.id}")
    markup = kb(
        [("🩺 Проверить", f"chk:{row.id}"), toggle],
        [("🔄 Обновить", f"b:{row.id}"), ("⬅️ К списку", back)],
    )
    return "\n".join(lines), markup


async def _back(state: FSMContext) -> str:
    return (await state.get_data()).get("back") or "l:new:all:0"


@router.callback_query(F.data.startswith("b:"))
async def on_card(callback: CallbackQuery, state: FSMContext) -> None:
    text, markup = await card_screen(int(callback.data.split(":")[1]), await _back(state))
    await show(callback, text, markup)
    await callback.answer()


@router.callback_query(F.data.startswith("chk:"))
async def on_check(callback: CallbackQuery, state: FSMContext) -> None:
    row_id = int(callback.data.split(":")[1])
    await callback.answer("Проверяю…")
    async with AsyncSessionLocal() as db:
        row = await find_copycat_bot(db, str(row_id))
        await db.commit()
    if row is None:
        return
    result = await service.health(row)
    text, markup = await card_screen(row_id, await _back(state), ("✅ " if result.ok else "⚠️ ") + esc(result.text))
    await show(callback, text, markup)


@router.callback_query(F.data.startswith("off:"))
async def on_disable_ask(callback: CallbackQuery) -> None:
    row_id = int(callback.data.split(":")[1])
    await callback.answer()
    await show(
        callback,
        f"Выключить витрину #{row_id}? Бот перестанет отвечать, вебхук снимется за минуту. "
        "Пользователи останутся, включить обратно можно в любой момент.",
        kb([("⏸ Да, выключить", f"off!:{row_id}"), ("Отмена", f"b:{row_id}")]),
    )


@router.callback_query(F.data.startswith("off!:") | F.data.startswith("on:"))
async def on_toggle(callback: CallbackQuery, state: FSMContext) -> None:
    action, _, raw_id = callback.data.partition(":")
    row_id = int(raw_id)
    enable = action == "on"
    async with AsyncSessionLocal() as db:
        row = await find_copycat_bot(db, str(row_id))
        if row is not None and row.id == row_id:
            row.enabled = enable
            await db.commit()
            logger.info(
                "Админка копикетов: %s %s витрину #%s @%s",
                callback.from_user.id,
                "включил" if enable else "выключил",
                row.id,
                row.username,
            )
    refresh_runtime_in_background()
    await callback.answer("Включена" if enable else "Выключена")
    notice = "▶️ Включена — поднимется за несколько секунд." if enable else "⏸ Выключена."
    text, markup = await card_screen(row_id, await _back(state), notice)
    await show(callback, text, markup)


# ---------------------------------------------------------------- поиск


@router.callback_query(F.data == "find")
async def on_find(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(FindStates.query)
    await callback.answer()
    await show(
        callback,
        "🔎 Пришлите @username, номер витрины (#id), Telegram id бота или его токен.",
        kb([("Отмена", "m")]),
    )


@router.message(FindStates.query, F.text)
async def on_find_query(message: Message, state: FSMContext) -> None:
    query = message.text.strip().lstrip("#")
    if "t.me/" in query:
        query = query.rsplit("/", 1)[-1]
    async with AsyncSessionLocal() as db:
        row = await find_copycat_bot(db, query)
        await db.commit()
    if row is None:
        await show(message, f"Не нашёл «{esc(query)}». Пришлите ещё раз.", kb([("Отмена", "m")]))
        return
    await state.set_state(None)
    text, markup = await card_screen(row.id, await _back(state))
    await show(message, text, markup)


# ---------------------------------------------------------------- добавление


@router.message(Command("add"))
async def on_add_command(message: Message, state: FSMContext) -> None:
    await _ask_token(message, state)


@router.callback_query(F.data == "add")
async def on_add(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _ask_token(callback, state)


async def _ask_token(event: Message | CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(AddStates.token)
    await show(
        event,
        "<b>➕ Новая витрина</b>\n\nПришлите токен бота — можно целиком ответ @BotFather.",
        kb([("Отмена", "m")]),
    )


@router.message(AddStates.token)
async def on_token(message: Message, state: FSMContext) -> None:
    token = find_token(message.text or message.caption)
    if token is None:
        await show(message, "Токена в сообщении нет. Пришлите токен вида <code>123456:ABC…</code>.", kb([("Отмена", "m")]))
        return
    try:
        await message.delete()  # токен не оставляем в истории чата
    except TelegramBadRequest:
        pass
    await message.bot.send_chat_action(message.chat.id, "typing")
    try:
        async with AsyncSessionLocal() as db:
            candidate = await service.check_token(db, token)
    except service.AddError as error:
        await show(message, f"⚠️ {esc(error)}\n\nПришлите другой токен.", kb([("Отмена", "m")]))
        return

    await state.update_data(
        token=candidate.token,
        bot_id=candidate.bot_id,
        username=candidate.username,
        title=candidate.title,
        foreign_webhook=candidate.foreign_webhook,
    )
    await state.set_state(AddStates.picture)
    warning = (
        f"\n\n⚠️ Сейчас у бота чужой вебхук: <code>{esc(candidate.foreign_webhook)}</code> — после добавления он перестанет туда ходить."
        if candidate.foreign_webhook
        else ""
    )
    await show(
        message,
        f"Бот <b>@{esc(candidate.username)}</b> — {esc(candidate.title)}\n"
        f"Имя бренда будет «{esc(clean_brand_name(candidate.title))}» (как в Telegram).{warning}\n\n"
        "Теперь картинка для приветствия: пришлите фото, файл или ссылку на картинку.",
        kb([("🖼 Взять аватарку бота", "add:avatar")], [("Отмена", "m")]),
    )


async def _accept_picture(event: Message | CallbackQuery, state: FSMContext, data: bytes, image_url: Optional[str]) -> None:
    draft = await state.get_data()
    try:
        logo = service.store_picture(data, draft["bot_id"])
    except service.AddError as error:
        await show(event, f"⚠️ {esc(error)}", kb([("🖼 Взять аватарку бота", "add:avatar")], [("Отмена", "m")]))
        return
    await state.update_data(logo=logo, image_url=image_url)
    await state.set_state(AddStates.confirm)
    with open(logo, "rb") as file:
        preview = file.read()
    await show(
        event,
        f"Добавить витрину <b>@{esc(draft['username'])}</b>?\n{esc(draft['title'])}\n"
        "Группа default, картинка приветствия — эта.",
        kb([("✅ Добавить", "add:ok")], [("🖼 Другая картинка", "add:repic"), ("Отмена", "m")]),
        photo=preview,
    )


@router.message(AddStates.picture, F.photo)
async def on_picture_photo(message: Message, state: FSMContext) -> None:
    buffer = await message.bot.download(message.photo[-1].file_id)
    await _accept_picture(message, state, buffer.read(), None)


@router.message(AddStates.picture, F.document)
async def on_picture_document(message: Message, state: FSMContext) -> None:
    if message.document.file_size and message.document.file_size > 10 * 1024 * 1024:
        await show(message, "⚠️ Файл больше 10 МБ.", kb([("Отмена", "m")]))
        return
    buffer = await message.bot.download(message.document.file_id)
    await _accept_picture(message, state, buffer.read(), None)


@router.message(AddStates.picture, F.text)
async def on_picture_url(message: Message, state: FSMContext) -> None:
    url = message.text.strip()
    if not url.startswith(("http://", "https://")):
        await show(message, "Пришлите фото, файл или ссылку http(s)://…", kb([("🖼 Взять аватарку бота", "add:avatar")], [("Отмена", "m")]))
        return
    try:
        data = await service.download(url)
    except service.AddError as error:
        await show(message, f"⚠️ {esc(error)}", kb([("🖼 Взять аватарку бота", "add:avatar")], [("Отмена", "m")]))
        return
    await _accept_picture(message, state, data, url)


@router.callback_query(AddStates.picture, F.data == "add:avatar")
async def on_picture_avatar(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    draft = await state.get_data()
    try:
        data = await service.avatar_bytes(draft["token"], draft["bot_id"])
    except service.AddError as error:
        await show(callback, f"⚠️ {esc(error)}", kb([("Отмена", "m")]))
        return
    await _accept_picture(callback, state, data, None)


@router.callback_query(AddStates.confirm, F.data == "add:repic")
async def on_repic(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(AddStates.picture)
    await show(callback, "Пришлите другую картинку: фото, файл или ссылку.", kb([("🖼 Взять аватарку бота", "add:avatar")], [("Отмена", "m")]))


@router.callback_query(AddStates.confirm, F.data == "add:ok")
async def on_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    draft = await state.get_data()
    candidate = service.Candidate(
        token=draft["token"],
        bot_id=draft["bot_id"],
        username=draft["username"],
        title=draft["title"],
        foreign_webhook=draft.get("foreign_webhook"),
    )
    try:
        async with AsyncSessionLocal() as db:
            # Пока выбирали картинку, бота могли завести с другого места.
            await service.check_token(db, candidate.token)
            row = await service.create(db, candidate, logo=draft["logo"], image_url=draft.get("image_url"))
    except service.AddError as error:
        await state.clear()
        await callback.answer()
        await show(callback, f"⚠️ {esc(error)}", kb([("🏠 Сводка", "m")]))
        return
    logger.info("Админка копикетов: %s добавил витрину #%s @%s", callback.from_user.id, row.id, row.username)
    await state.clear()
    await state.update_data(back="l:new:all:0")
    refresh_runtime_in_background()
    await callback.answer("Добавлена")
    text, markup = await card_screen(
        row.id,
        "l:new:all:0",
        "✅ Добавлена. Бот поднимется и получит вебхук за несколько секунд — нажмите «Проверить».",
    )
    await show(callback, text, markup)


@router.callback_query(F.data.startswith("add:"))
async def on_stale_add(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer("Это добавление уже закончено", show_alert=True)


@router.message()
async def on_other(message: Message, state: FSMContext) -> None:
    await state.clear()
    text, markup = await summary_screen()
    await show(message, text, markup)
