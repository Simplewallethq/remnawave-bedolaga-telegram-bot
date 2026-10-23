"""Запуск бота-конструктора в процессе основного бота.

Свой Bot и свой Dispatcher: VPN-обвязка основного диспетчера (регистрация,
подписки, техработы, каналы) конструктору не нужна. FSM-хранилище общее —
ключи разведены по id бота. Вебхук — `<WEBHOOK_PATH>/constructor`, в
polling-режиме — свой поллинг. Мини-апп и его API — `/constructor/…` того же
веб-сервера.

Любой сбой конструктора пишется в лог и не мешает основному боту.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.base import BaseStorage
from aiogram.types import BotCommand, MenuButtonWebApp, WebAppInfo
from fastapi import FastAPI

from app.config import settings
from app.services import owner_platform_service as platform

logger = logging.getLogger(__name__)

URL_PREFIX = "/constructor"


def webhook_path() -> str:
    return settings.get_telegram_webhook_path().rstrip("/") + URL_PREFIX


class ConstructorRuntime:
    def __init__(self) -> None:
        self.bot: Optional[Bot] = None
        self.username: Optional[str] = None
        self.dispatcher: Optional[Dispatcher] = None
        self.ui = None
        self._polling: Optional[asyncio.Task] = None

    @property
    def bot_id(self) -> Optional[int]:
        return self.bot.id if self.bot else None

    def redirect_url(self) -> Optional[str]:
        """Ссылка на конструктор для кнопок бота-платформы — когда создание
        ботов переведено туда (OWNER_PLATFORM_REDIRECT_TO_CONSTRUCTOR)."""
        if not settings.OWNER_PLATFORM_REDIRECT_TO_CONSTRUCTOR or not self.username:
            return None
        return f"https://t.me/{self.username}"

    async def setup(self, storage: BaseStorage) -> bool:
        """Поднимает бота и диспетчер. False — конструктор выключен или не поднялся."""
        if not settings.is_constructor_enabled():
            return False
        from .handlers import router
        from .ui import UI, Sessions

        bot = Bot(token=settings.CONSTRUCTOR_BOT_TOKEN.strip())
        try:
            me = await bot.get_me()
        except Exception as error:  # noqa: BLE001
            logger.error("Конструктор не поднялся: токен не прошёл getMe: %s", error)
            await bot.session.close()
            return False
        platform.reserve_bot_id(me.id)

        self.ui = UI(bot, Sessions(storage, me.id))
        dispatcher = Dispatcher(storage=storage, ui=self.ui)
        dispatcher.include_router(router)
        self.bot, self.dispatcher, self.username = bot, dispatcher, me.username
        logger.info("Конструктор: @%s (id=%s)", me.username, me.id)

        try:
            await bot.set_my_commands([BotCommand(command="start", description="Главное меню")])
            url = (settings.CONSTRUCTOR_WEBAPP_URL or "").strip()
            if url:
                await bot.set_chat_menu_button(
                    menu_button=MenuButtonWebApp(text="Открыть", web_app=WebAppInfo(url=url))
                )
        except Exception as error:  # noqa: BLE001 - меню вторично
            logger.warning("Конструктор: не удалось настроить меню: %s", error)
        return True

    def mount(self, app: FastAPI, *, enable_webhook: bool) -> None:
        """Вебхук бота, API мини-аппа и его статика в общем веб-сервере.

        API подключается, даже если бот не поднялся: мини-апп проверяет
        подпись initData токеном из настроек и работает с базой напрямую."""
        if not settings.is_constructor_enabled():
            return
        from fastapi.staticfiles import StaticFiles

        from app.webserver.telegram import TelegramWebhookProcessor, create_telegram_router

        from . import api

        app.include_router(api.router, prefix=URL_PREFIX + "/api")

        if enable_webhook and self.bot is not None:
            processor = TelegramWebhookProcessor(
                bot=self.bot,
                dispatcher=self.dispatcher,
                queue_maxsize=settings.get_webhook_queue_maxsize(),
                worker_count=settings.get_webhook_worker_count(),
                enqueue_timeout=settings.get_webhook_enqueue_timeout(),
                shutdown_timeout=settings.get_webhook_shutdown_timeout(),
            )
            router = create_telegram_router(self.bot, self.dispatcher, processor=processor, webhook_path=webhook_path())
            # Health-маршрут основного бота уже есть — оставляем только вебхук.
            router.routes = [route for route in router.routes if getattr(route, "path", "") == webhook_path()]
            app.include_router(router)
            app.add_event_handler("startup", processor.start)
            app.add_event_handler("shutdown", processor.stop)

        static = Path(settings.CONSTRUCTOR_WEBAPP_STATIC_PATH)
        if static.exists():
            app.mount(URL_PREFIX, StaticFiles(directory=static, html=True), name="constructor-webapp")
        else:
            logger.info("Конструктор: сборки мини-аппа нет (%s), статика не отдаётся", static)

    async def set_webhook(self) -> None:
        if self.bot is None:
            return
        base_url = (settings.WEBHOOK_URL or "").strip()
        if not base_url:
            logger.warning("Конструктор: WEBHOOK_URL не задан, вебхук не установлен")
            return
        url = base_url.rstrip("/") + webhook_path()
        try:
            await self.bot.set_webhook(
                url=url,
                secret_token=settings.WEBHOOK_SECRET_TOKEN,
                allowed_updates=self.dispatcher.resolve_used_update_types(),
            )
            logger.info("Конструктор: вебхук %s", url)
        except Exception as error:  # noqa: BLE001
            logger.error("Конструктор: вебхук не установлен: %s", error)

    def start_polling(self) -> None:
        if self.bot is None or self._polling is not None:
            return
        self._polling = asyncio.create_task(
            self.dispatcher.start_polling(self.bot, handle_signals=False, close_bot_session=False)
        )

    async def stop(self, *, delete_webhook: bool) -> None:
        if self.bot is None:
            return
        if self._polling is not None:
            self._polling.cancel()
            try:
                await self._polling
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._polling = None
        if delete_webhook:
            try:
                await self.bot.delete_webhook(drop_pending_updates=False)
            except Exception as error:  # noqa: BLE001
                logger.error("Конструктор: вебхук не снят: %s", error)
        await self.bot.session.close()


constructor_runtime = ConstructorRuntime()
