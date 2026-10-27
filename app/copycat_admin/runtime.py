"""Запуск бота-админки копикетов в процессе основного бота.

Как у конструктора: свой Bot и свой Dispatcher без VPN-обвязки основного, FSM
общее (ключи разведены по id бота), вебхук — `<WEBHOOK_PATH>/copycat-admin`,
в polling-режиме — свой поллинг. Сбой админки не мешает основному боту.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.base import BaseStorage
from aiogram.types import BotCommand
from fastapi import FastAPI

from app.config import settings
from app.services import owner_platform_service as platform

logger = logging.getLogger(__name__)

URL_SUFFIX = "/copycat-admin"


def webhook_path() -> str:
    return settings.get_telegram_webhook_path().rstrip("/") + URL_SUFFIX


class CopycatAdminRuntime:
    def __init__(self) -> None:
        self.bot: Optional[Bot] = None
        self.dispatcher: Optional[Dispatcher] = None
        self._polling: Optional[asyncio.Task] = None

    async def setup(self, storage: BaseStorage) -> bool:
        """Поднимает бота и диспетчер. False — админка выключена или не поднялась."""
        if not settings.is_copycat_admin_enabled():
            return False
        from .handlers import AccessMiddleware, router

        bot = Bot(
            token=settings.COPYCAT_ADMIN_BOT_TOKEN.strip(),
            default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
        )
        try:
            me = await bot.get_me()
        except Exception as error:  # noqa: BLE001
            logger.error("Админка копикетов не поднялась: токен не прошёл getMe: %s", error)
            await bot.session.close()
            return False
        platform.reserve_bot_id(me.id)

        dispatcher = Dispatcher(storage=storage)
        dispatcher.message.outer_middleware(AccessMiddleware())
        dispatcher.callback_query.outer_middleware(AccessMiddleware())
        dispatcher.include_router(router)
        self.bot, self.dispatcher = bot, dispatcher
        logger.info(
            "Админка копикетов: @%s (id=%s), доступ: %s",
            me.username,
            me.id,
            ", ".join(map(str, sorted(settings.get_copycat_admin_user_ids()))) or "никому",
        )
        try:
            await bot.set_my_commands(
                [
                    BotCommand(command="start", description="Сводка"),
                    BotCommand(command="list", description="Список витрин"),
                    BotCommand(command="add", description="Добавить витрину"),
                ]
            )
        except Exception as error:  # noqa: BLE001 - меню вторично
            logger.warning("Админка копикетов: не удалось настроить команды: %s", error)
        return True

    def mount(self, app: FastAPI, *, enable_webhook: bool) -> None:
        if not enable_webhook or self.bot is None:
            return
        from app.webserver.telegram import TelegramWebhookProcessor, create_telegram_router

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

    async def set_webhook(self) -> None:
        if self.bot is None:
            return
        base_url = (settings.WEBHOOK_URL or "").strip()
        if not base_url:
            logger.warning("Админка копикетов: WEBHOOK_URL не задан, вебхук не установлен")
            return
        url = base_url.rstrip("/") + webhook_path()
        try:
            await self.bot.set_webhook(
                url=url,
                secret_token=settings.WEBHOOK_SECRET_TOKEN,
                allowed_updates=self.dispatcher.resolve_used_update_types(),
            )
            logger.info("Админка копикетов: вебхук %s", url)
        except Exception as error:  # noqa: BLE001
            logger.error("Админка копикетов: вебхук не установлен: %s", error)

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
                logger.error("Админка копикетов: вебхук не снят: %s", error)
        await self.bot.session.close()


copycat_admin_runtime = CopycatAdminRuntime()
