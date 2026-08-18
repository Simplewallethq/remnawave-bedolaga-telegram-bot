import logging
from aiogram import Bot, Dispatcher, types
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.fsm.storage.memory import MemoryStorage
import redis.asyncio as redis

from app.config import settings
from app.middlewares.global_error import GlobalErrorMiddleware
from app.middlewares.private_chat_only import PrivateChatOnlyMiddleware
from app.middlewares.auth import AuthMiddleware
from app.middlewares.logging import LoggingMiddleware
from app.middlewares.throttling import ThrottlingMiddleware
from app.middlewares.subscription_checker import SubscriptionStatusMiddleware
from app.middlewares.maintenance import MaintenanceMiddleware
from app.middlewares.display_name_restriction import DisplayNameRestrictionMiddleware
from app.middlewares.button_stats import ButtonStatsMiddleware
from app.services.maintenance_service import maintenance_service
from app.utils.cache import cache 

from app.handlers import (
    commands,
    start,
    menu,
    subscription,
    balance,
    promocode,
    rays_shop,
    referral,
    referral_withdrawals,
    support,
    server_status,
    common,
    feedback,
    tickets,
    android_rate_request,
)
from app.handlers import polls as user_polls
from app.handlers import simple_subscription
from app.handlers.admin import (
    main as admin_main,
    blacklist as admin_blacklist,
    bulk_ban as admin_bulk_ban,
    users as admin_users,
    subscriptions as admin_subscriptions,
    promocodes as admin_promocodes,
    messages as admin_messages,
    cabinet_notify as admin_cabinet_notify,
    monitoring as admin_monitoring,
    referrals as admin_referrals,
    rules as admin_rules,
    remnawave as admin_remnawave,
    statistics as admin_statistics,
    stars_topup as admin_stars_topup,
    polls as admin_polls,
    servers as admin_servers,
    maintenance as admin_maintenance,
    promo_groups as admin_promo_groups,
    campaigns as admin_campaigns,
    contests as admin_contests,
    daily_contests as admin_daily_contests,
    promo_offers as admin_promo_offers,
    user_messages as admin_user_messages,
    updates as admin_updates,
    backup as admin_backup,
    system_logs as admin_system_logs,
    welcome_text as admin_welcome_text,
    tickets as admin_tickets,
    reports as admin_reports,
    bot_configuration as admin_bot_configuration,
    pricing as admin_pricing,
    privacy_policy as admin_privacy_policy,
    public_offer as admin_public_offer,
    faq as admin_faq,
    payments as admin_payments,
    trials as admin_trials,
)
from app.handlers import contests as user_contests
from app.handlers.stars_payments import register_stars_handlers

from app.utils.message_patch import patch_message_methods

patch_message_methods()

logger = logging.getLogger(__name__)


async def debug_callback_handler(callback: types.CallbackQuery):
    logger.info(f"🔍 DEBUG CALLBACK:")
    logger.info(f"  - Data: {callback.data}")
    logger.info(f"  - User: {callback.from_user.id}")
    logger.info(f"  - Username: {callback.from_user.username}")


async def setup_bot() -> tuple[list[Bot], Dispatcher]:

    try:
        await cache.connect()
        logger.info("Кеш инициализирован")
    except Exception as e:
        logger.warning(f"Кеш не инициализирован: {e}")

    from pathlib import Path
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    from app.utils import bot_registry

    _default = DefaultBotProperties(parse_mode=ParseMode.HTML)

    primary_bot = Bot(token=settings.BOT_TOKEN, default=_default)
    primary_bot_id = (await primary_bot.get_me()).id
    bot_registry.register_bot(primary_bot_id, Path(settings.LOGO_FILE))
    logger.info("Primary bot registered: id=%s", primary_bot_id)

    all_bots: list[Bot] = [primary_bot]
    for mirror_cfg in settings.get_mirror_bots():
        try:
            m_bot = Bot(token=mirror_cfg["token"], default=_default)
            m_me = await m_bot.get_me()
            bot_registry.register_bot(m_me.id, Path(mirror_cfg["logo"]))
            all_bots.append(m_bot)
            logger.info(
                "Mirror bot registered: @%s id=%s logo=%s",
                m_me.username, m_me.id, mirror_cfg["logo"],
            )
        except Exception as exc:
            logger.error(
                "Failed to init mirror bot (token %s...): %s",
                mirror_cfg["token"][:10], exc,
            )

    maintenance_service.set_bot(primary_bot)
    logger.info("Бот установлен в maintenance_service")
    
    try:
        redis_client = redis.from_url(settings.REDIS_URL)
        await redis_client.ping()
        storage = RedisStorage(redis_client)
        logger.info("Подключено к Redis для FSM storage")
    except Exception as e:
        logger.warning(f"Не удалось подключиться к Redis: {e}")
        logger.info("Используется MemoryStorage для FSM")
        storage = MemoryStorage()
    
    
    dp = Dispatcher(storage=storage)

    dp.message.middleware(PrivateChatOnlyMiddleware())
    dp.callback_query.middleware(PrivateChatOnlyMiddleware())
    dp.message.middleware(GlobalErrorMiddleware())
    dp.callback_query.middleware(GlobalErrorMiddleware())
    dp.pre_checkout_query.middleware(GlobalErrorMiddleware())
    dp.message.middleware(LoggingMiddleware())
    dp.callback_query.middleware(LoggingMiddleware())
    dp.message.middleware(MaintenanceMiddleware())
    dp.callback_query.middleware(MaintenanceMiddleware())
    display_name_middleware = DisplayNameRestrictionMiddleware()
    dp.message.middleware(display_name_middleware)
    dp.callback_query.middleware(display_name_middleware)
    dp.pre_checkout_query.middleware(display_name_middleware)
    dp.message.middleware(ThrottlingMiddleware())
    dp.callback_query.middleware(ThrottlingMiddleware())
    
    # Middleware для автоматического логирования кликов по кнопкам
    if settings.MENU_LAYOUT_ENABLED:
        button_stats_middleware = ButtonStatsMiddleware()
        dp.callback_query.middleware(button_stats_middleware)
        logger.info("📊 ButtonStatsMiddleware активирован")

    if settings.CHANNEL_IS_REQUIRED_SUB:
        from app.middlewares.channel_checker import ChannelCheckerMiddleware

        channel_checker_middleware = ChannelCheckerMiddleware()
        dp.message.middleware(channel_checker_middleware)
        dp.callback_query.middleware(channel_checker_middleware)
        logger.info("🔒 Обязательная подписка включена - ChannelCheckerMiddleware активирован")
    else:
        logger.info("🔓 Обязательная подписка отключена - ChannelCheckerMiddleware не зарегистрирован")
    dp.message.middleware(AuthMiddleware())
    dp.callback_query.middleware(AuthMiddleware())
    dp.pre_checkout_query.middleware(AuthMiddleware())
    dp.message.middleware(SubscriptionStatusMiddleware())
    dp.callback_query.middleware(SubscriptionStatusMiddleware())
    commands.register_handlers(dp)
    start.register_handlers(dp)
    menu.register_handlers(dp)
    subscription.register_handlers(dp)
    balance.register_balance_handlers(dp)
    promocode.register_handlers(dp)
    rays_shop.register_handlers(dp)
    referral.register_handlers(dp)
    referral_withdrawals.register_handlers(dp)
    support.register_handlers(dp)
    server_status.register_handlers(dp)
    tickets.register_handlers(dp)
    admin_main.register_handlers(dp)
    admin_stars_topup.register_handlers(dp)
    admin_users.register_handlers(dp)
    admin_subscriptions.register_handlers(dp)
    admin_servers.register_handlers(dp)  
    admin_promocodes.register_handlers(dp)
    admin_messages.register_handlers(dp)
    admin_cabinet_notify.register_handlers(dp)
    admin_monitoring.register_handlers(dp)
    admin_referrals.register_handlers(dp)
    admin_rules.register_handlers(dp)
    admin_remnawave.register_handlers(dp)
    admin_statistics.register_handlers(dp)
    admin_polls.register_handlers(dp)
    admin_promo_groups.register_handlers(dp)
    admin_campaigns.register_handlers(dp)
    admin_contests.register_handlers(dp)
    admin_daily_contests.register_handlers(dp)
    admin_promo_offers.register_handlers(dp)
    admin_maintenance.register_handlers(dp)
    admin_user_messages.register_handlers(dp)
    admin_updates.register_handlers(dp)
    admin_backup.register_handlers(dp)
    admin_system_logs.register_handlers(dp)
    admin_welcome_text.register_welcome_text_handlers(dp)
    admin_tickets.register_handlers(dp)
    admin_reports.register_handlers(dp)
    admin_bot_configuration.register_handlers(dp)
    admin_pricing.register_handlers(dp)
    admin_privacy_policy.register_handlers(dp)
    admin_public_offer.register_handlers(dp)
    admin_faq.register_handlers(dp)
    admin_payments.register_handlers(dp)
    admin_trials.register_handlers(dp)
    admin_bulk_ban.register_bulk_ban_handlers(dp)
    admin_blacklist.register_blacklist_handlers(dp)
    feedback.register_handlers(dp)
    android_rate_request.register_handlers(dp)
    common.register_handlers(dp)
    register_stars_handlers(dp)
    user_contests.register_handlers(dp)
    user_polls.register_handlers(dp)
    simple_subscription.register_simple_subscription_handlers(dp)
    logger.info("⭐ Зарегистрированы обработчики Telegram Stars платежей")
    logger.info("⚡ Зарегистрированы обработчики простой покупки")
    logger.info("⚡ Зарегистрированы обработчики простой подписки")
    
    if settings.is_maintenance_monitoring_enabled():
        try:
            await maintenance_service.start_monitoring()
            logger.info("Мониторинг техработ запущен")
        except Exception as e:
            logger.error(f"Ошибка запуска мониторинга техработ: {e}")
    else:
        logger.info("Мониторинг техработ отключен настройками")
    
    logger.info("🛡️ GlobalErrorMiddleware активирован - бот защищен от устаревших callback queries")
    logger.info("Бот успешно настроен")
    
    return all_bots, dp


async def shutdown_bot():
    try:
        await maintenance_service.stop_monitoring()
        logger.info("Мониторинг техработ остановлен")
    except Exception as e:
        logger.error(f"Ошибка остановки мониторинга: {e}")
    
    try:
        await cache.close()
        logger.info("Соединения с кешем закрыты")
    except Exception as e:
        logger.error(f"Ошибка закрытия кеша: {e}")
