"""aiogram-хендлеры бота-конструктора: /start, кнопки и ввод.

Любое сообщение пользователя (токен, username, картинка, случайный текст)
сразу удаляется, ответ показывается в том же единственном сообщении бота.
"""

from __future__ import annotations

import logging
from typing import Optional

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message, User

from app.database.database import AsyncSessionLocal

from . import screens, service
from .ui import UI

logger = logging.getLogger(__name__)

router = Router(name="constructor")
router.message.filter(F.chat.type == ChatType.PRIVATE)


def tg_user(user: User) -> service.TgUser:
    return service.TgUser(
        id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
        language_code=user.language_code,
    )


async def render(
    ui: UI,
    ctx: screens.Ctx,
    route: str,
    *,
    error: Optional[str] = None,
    notice: Optional[str] = None,
    fresh: bool = False,
) -> None:
    """Построить экран маршрута и показать его в единственном сообщении."""
    for _ in range(5):  # защита от циклов редиректов
        found = screens.resolve(route)
        if found is None:
            route, error, notice = "main", None, None
            continue
        build, _, match = found
        ctx.match, ctx.error, ctx.notice = match, error, notice
        try:
            screen = await build(ctx)
        except screens.Redirect as redirect:
            route, error, notice = redirect.route, redirect.error, None
            continue
        ctx.session["route"] = route
        await ui.show(ctx.uid, ctx.session, screen, fresh=fresh)
        return
    raise RuntimeError(f"Конструктор: цикл редиректов на {route}")


@router.message(F.text.startswith("/start"))
async def on_start(message: Message, ui: UI) -> None:
    await ui.delete(message.chat.id, message.message_id)
    uid = message.from_user.id
    async with ui.lock(uid), AsyncSessionLocal() as db:
        session = await ui.sessions.load(uid)
        if str(session.get("route", "")).startswith("wiz"):
            session["draft"] = None  # /start прерывает мастер
        ctx = screens.Ctx(bot=message.bot, ui=ui, db=db, tg=tg_user(message.from_user), session=session)
        try:
            # /start опускает интерфейс вниз чата: новое сообщение, старое удаляется
            await render(ui, ctx, "main", fresh=True)
        finally:
            await ui.sessions.save(uid, session)


@router.callback_query(F.data)
async def on_callback(callback: CallbackQuery, ui: UI) -> None:
    uid = callback.from_user.id
    async with ui.lock(uid), AsyncSessionLocal() as db:
        session = await ui.sessions.load(uid)
        # Нажали кнопку на старом сообщении — оно становится главным, прежнее удаляется.
        if callback.message and session.get("msg_id") != callback.message.message_id:
            if session.get("msg_id"):
                await ui.delete(uid, session["msg_id"])
            session["msg_id"] = callback.message.message_id

        ctx = screens.Ctx(bot=callback.bot, ui=ui, db=db, tg=tg_user(callback.from_user), session=session)
        kind, _, rest = callback.data.partition(":")
        route, error = rest, None
        try:
            if kind == "a":
                name, _, arg = rest.partition(":")
                handler = screens.get_action(name)
                route = await handler(ctx, arg) if handler else "main"
        except screens.Redirect as redirect:
            route, error = redirect.route, redirect.error
        try:
            try:
                await callback.answer(ctx.toast or None)
            except TelegramBadRequest:
                pass
            await render(ui, ctx, route, error=error, notice=ctx.notice)
        finally:
            await ui.sessions.save(uid, session)


@router.message()
async def on_message(message: Message, ui: UI) -> None:
    """Любое сообщение: прочитать → удалить → обновить главное сообщение."""
    uid = message.from_user.id
    await ui.delete(message.chat.id, message.message_id)
    async with ui.lock(uid), AsyncSessionLocal() as db:
        session = await ui.sessions.load(uid)
        ctx = screens.Ctx(bot=message.bot, ui=ui, db=db, tg=tg_user(message.from_user), session=session)
        route = session.get("route") or "main"
        found = screens.resolve(route)
        try:
            if found is None or found[1] is None:
                # Ввод не ожидается: сообщение уже удалено, показываем текущий экран.
                await render(ui, ctx, route, error="Пользуйтесь кнопками под сообщением")
                return
            ctx.match = found[2]
            try:
                route = await found[1](ctx, message)
            except screens.InputError as error:
                await render(ui, ctx, route, error=str(error))
                return
            except screens.Redirect as redirect:
                await render(ui, ctx, redirect.route, error=redirect.error)
                return
            await render(ui, ctx, route, notice=ctx.notice)
        finally:
            await ui.sessions.save(uid, session)
