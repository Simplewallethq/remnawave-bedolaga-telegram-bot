from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from aiogram import Bot
from aiogram import Dispatcher

from app.config import settings
from app.services.payment_service import PaymentService
from app.webapi.app import create_web_api_app
from app.webapi.routes import cabinet as cabinet_routes
from app.webapi.docs import add_redoc_endpoint

from . import payments
from . import telegram
from . import android_rate_request


logger = logging.getLogger(__name__)


def _attach_docs_alias(app: FastAPI, docs_url: str | None) -> None:
    if not docs_url:
        return

    alias_path = "/doc"
    if alias_path == docs_url:
        return

    for route in app.router.routes:
        if getattr(route, "path", None) == alias_path:
            return

    target_url = docs_url

    @app.get(alias_path, include_in_schema=False)
    async def redirect_doc() -> RedirectResponse:  # pragma: no cover - simple redirect
        return RedirectResponse(url=target_url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)


def _create_base_app() -> FastAPI:
    docs_config = settings.get_web_api_docs_config()

    if settings.is_web_api_enabled():
        app = create_web_api_app()
    else:
        app = FastAPI(
            title="Bedolaga Unified Server",
            version=settings.WEB_API_VERSION,
            docs_url=docs_config.get("docs_url"),
            redoc_url=None,
            openapi_url=docs_config.get("openapi_url"),
        )

        add_redoc_endpoint(
            app,
            redoc_url=docs_config.get("redoc_url"),
            openapi_url=docs_config.get("openapi_url"),
            title="Bedolaga Unified Server",
        )

    _attach_docs_alias(app, app.docs_url)
    return app


def _mount_miniapp_static(app: FastAPI) -> tuple[bool, Path]:
    static_path: Path = settings.get_miniapp_static_path()
    if not static_path.exists():
        logger.debug("Miniapp static path %s does not exist, skipping mount", static_path)
        return False, static_path

    try:
        app.mount("/miniapp/static", StaticFiles(directory=static_path), name="miniapp-static")
        logger.info("📦 Miniapp static files mounted at /miniapp/static from %s", static_path)
    except RuntimeError as error:  # pragma: no cover - defensive guard
        logger.warning("Не удалось смонтировать статические файлы миниаппа: %s", error)
        return False, static_path

    return True, static_path


def create_unified_app(
    all_bots: list[Bot],
    dispatcher: Dispatcher,
    payment_service: PaymentService,
    *,
    enable_telegram_webhook: bool,
) -> FastAPI:
    app = _create_base_app()

    # Личный кабинет (LetoVPNSite). Когда админский Web API включён, cabinet-роутер
    # и CORS уже подключены внутри create_web_api_app(). Когда выключен — базовое
    # приложение их не содержит, поэтому монтируем здесь, чтобы /cabinet/* работал
    # независимо от админки (иначе фронт ловит CORS-ошибку на 404 без заголовков).
    if settings.CABINET_ENABLED and not settings.is_web_api_enabled():
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.get_web_api_allowed_origins(),
            allow_credentials=False,  # кабинет авторизуется Bearer-токеном, не cookies
            allow_methods=["*"],
            allow_headers=["*"],
        )
        app.include_router(cabinet_routes.router, prefix="/cabinet", tags=["cabinet"])

    bot = all_bots[0]
    app.state.bot = bot
    app.state.dispatcher = dispatcher
    app.state.payment_service = payment_service

    payments_router = payments.create_payment_router(bot, payment_service)
    if payments_router:
        app.include_router(payments_router)
    app.include_router(android_rate_request.router)
    payment_providers_state = {
        "tribute": settings.TRIBUTE_ENABLED,
        "mulenpay": settings.is_mulenpay_enabled(),
        "cryptobot": settings.is_cryptobot_enabled(),
        "yookassa": settings.is_yookassa_enabled(),
        "pal24": settings.is_pal24_enabled(),
        "wata": settings.is_wata_enabled(),
        "heleket": settings.is_heleket_enabled(),
    }

    if enable_telegram_webhook:
        processors: list[telegram.TelegramWebhookProcessor] = []
        primary_path = settings.get_telegram_webhook_path()
        for idx, b in enumerate(all_bots):
            path = primary_path if idx == 0 else f"{primary_path}/mirror/{idx}"
            proc = telegram.TelegramWebhookProcessor(
                bot=b,
                dispatcher=dispatcher,
                queue_maxsize=settings.get_webhook_queue_maxsize(),
                worker_count=settings.get_webhook_worker_count(),
                enqueue_timeout=settings.get_webhook_enqueue_timeout(),
                shutdown_timeout=settings.get_webhook_shutdown_timeout(),
            )
            processors.append(proc)
            app.include_router(
                telegram.create_telegram_router(b, dispatcher, processor=proc, webhook_path=path)
            )

        telegram_processor = processors[0] if processors else None
        app.state.telegram_webhook_processor = telegram_processor

        @app.on_event("startup")
        async def start_telegram_webhook_processors() -> None:  # pragma: no cover - event hook
            for proc in processors:
                await proc.start()

        @app.on_event("shutdown")
        async def stop_telegram_webhook_processors() -> None:  # pragma: no cover - event hook
            for proc in processors:
                await proc.stop()
    else:
        telegram_processor = None

    miniapp_mounted, miniapp_path = _mount_miniapp_static(app)

    unified_health_path = "/health/unified" if settings.is_web_api_enabled() else "/health"

    @app.get(unified_health_path)
    async def unified_health() -> JSONResponse:
        webhook_path = settings.get_telegram_webhook_path() if enable_telegram_webhook else None

        telegram_state = {
            "enabled": enable_telegram_webhook,
            "running": bool(telegram_processor and telegram_processor.is_running),
            "url": settings.get_telegram_webhook_url(),
            "path": webhook_path,
            "secret_configured": bool(settings.WEBHOOK_SECRET_TOKEN),
            "queue_maxsize": settings.get_webhook_queue_maxsize(),
            "workers": settings.get_webhook_worker_count(),
        }

        payment_state = {
            "enabled": bool(payments_router),
            "providers": payment_providers_state,
        }

        miniapp_state = {
            "mounted": miniapp_mounted,
            "path": str(miniapp_path),
        }

        return JSONResponse(
            {
                "status": "ok",
                "bot_run_mode": settings.get_bot_run_mode(),
                "web_api_enabled": settings.is_web_api_enabled(),
                "payment_webhooks": payment_state,
                "telegram_webhook": telegram_state,
                "miniapp_static": miniapp_state,
            }
        )

    return app
