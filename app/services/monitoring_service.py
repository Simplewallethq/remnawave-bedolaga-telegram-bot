import asyncio
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Any, Optional, Set, Tuple

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.enums import ButtonStyle, ChatMemberStatus
from aiogram.types import FSInputFile
from sqlalchemy import select, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.database import get_db
from app.database.crud.discount_offer import (
    deactivate_expired_offers,
    get_latest_claimed_offer_for_user,
    upsert_discount_offer,
)
from app.database.crud.promo_offer_log import log_promo_offer_action
from app.database.crud.notification import (
    clear_notification_by_type,
    notification_sent,
    record_notification,
)
from app.database.crud.subscription import (
    deactivate_subscription,
    extend_subscription,
    get_expired_subscriptions,
    get_expiring_subscriptions,
    get_subscriptions_for_autopay,
)
from app.database.crud.subscription_event import record_subscription_renewal_event
from app.database.crud.user import (
    delete_user,
    get_inactive_users,
    get_user_by_id,
    subtract_user_balance,
    cleanup_expired_promo_offer_discounts,
)
from app.utils.timezone import format_local_datetime
from app.utils.subscription_utils import (
    resolve_hwid_device_limit_for_payload,
)
from app.database.models import (
    MonitoringLog,
    SubscriptionStatus,
    Subscription,
    User,
    Ticket,
    TicketStatus,
    UserPromoGroup,
)
from app.localization.texts import get_texts
from app.services.notification_settings_service import NotificationSettingsService
from app.services.payment_service import PaymentService
from app.services.remnawave_service import RemnaWaveService
from app.services.subscription_service import SubscriptionService
from app.services.promo_offer_service import promo_offer_service
from app.services.user_daily_traffic_usage_service import user_daily_traffic_usage_service
from app.services.daily_subscription_metrics_service import daily_subscription_metrics_service
from app.services.user_daily_metrics_service import user_daily_metrics_service
from app.services.trial_expiry_daily_metrics_service import trial_expiry_daily_metrics_service
from app.services.android_rate_request_service import android_rate_request_service
from app.services.expired_subscription_feedback_service import expired_subscription_feedback_service
from app.utils.pricing_utils import apply_percentage_discount
from app.utils.miniapp_buttons import build_miniapp_or_callback_button

from app.external.remnawave_api import (
    RemnaWaveAPIError,
    RemnaWaveUser,
    TrafficLimitStrategy,
    UserStatus,
)

from app.utils.bot_registry import get_primary_logo

logger = logging.getLogger(__name__)


SUBSCRIPTION_EXPIRING_IMAGE = Path("images") / "subend.webp"


class MonitoringService:
    SEND_LEGACY_EXPIRED_TELEGRAM_NOTIFICATIONS = False
    SEND_LEGACY_EXPIRED_FOLLOWUPS = False
    REMNAWAVE_SYNC_INTERVAL_MINUTES = 15
    
    def __init__(self, bot=None):
        self.is_running = False
        self.subscription_service = SubscriptionService()
        self.payment_service = PaymentService()
        self.user_daily_traffic_usage_service = user_daily_traffic_usage_service
        self.daily_subscription_metrics_service = daily_subscription_metrics_service
        self.user_daily_metrics_service = user_daily_metrics_service
        self.trial_expiry_daily_metrics_service = trial_expiry_daily_metrics_service
        self.bot = bot
        self._notified_users: Set[str] = set()
        # subscription_id -> end_date, на который уже уходило сообщение о
        # неудачном автоплатеже. Живет дольше _notified_users (тот чистится
        # раз в час), чтобы не слать по сообщению каждый цикл мониторинга.
        self._autopay_failed_notified: Dict[int, datetime] = {}
        self._last_cleanup = datetime.utcnow()
        self._last_cabinet_notifications_cleanup = datetime.min
        self._sla_task = None
        self._last_remnawave_sync_at: Optional[datetime] = None
        self._remnawave_sync_lock = asyncio.Lock()

    async def _send_message_with_logo(
        self,
        chat_id: int,
        text: str,
        reply_markup=None,
        parse_mode: Optional[str] = "HTML",
    ):
        """Отправляет сообщение, добавляя логотип при необходимости."""
        if not self.bot:
            raise RuntimeError("Bot instance is not available")

        logo_path = get_primary_logo()
        if (
            settings.ENABLE_LOGO_MODE
            and logo_path.exists()
            and (text is None or len(text) <= 1000)
        ):
            try:
                return await self.bot.send_photo(
                    chat_id=chat_id,
                    photo=FSInputFile(logo_path),
                    caption=text,
                    reply_markup=reply_markup,
                    parse_mode=parse_mode,
                )
            except TelegramBadRequest as exc:
                logger.warning(
                    "Не удалось отправить сообщение с логотипом пользователю %s: %s. "
                    "Отправляем текстовое сообщение.",
                    chat_id,
                    exc,
                )

        return await self.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )

    async def _send_message_with_image(
        self,
        chat_id: int,
        text: str,
        image_path: Path,
        reply_markup=None,
        parse_mode: Optional[str] = "HTML",
    ):
        """Отправляет сообщение с заданной картинкой, иначе — с логотипом."""
        if not self.bot:
            raise RuntimeError("Bot instance is not available")

        if image_path.exists() and (text is None or len(text) <= 1000):
            try:
                return await self.bot.send_photo(
                    chat_id=chat_id,
                    photo=FSInputFile(image_path),
                    caption=text,
                    reply_markup=reply_markup,
                    parse_mode=parse_mode,
                )
            except TelegramBadRequest as exc:
                logger.warning(
                    "Не удалось отправить сообщение с картинкой %s пользователю %s: %s. "
                    "Откатываемся на логотип.",
                    image_path,
                    chat_id,
                    exc,
                )

        return await self._send_message_with_logo(
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )

    @staticmethod
    def _is_unreachable_error(error: TelegramBadRequest) -> bool:
        message = str(error).lower()
        unreachable_markers = (
            "chat not found",
            "user is deactivated",
            "bot was blocked by the user",
            "bot can't initiate conversation",
            "can't initiate conversation",
            "user not found",
            "peer id invalid",
        )
        return any(marker in message for marker in unreachable_markers)

    def _handle_unreachable_user(self, user: User, error: Exception, context: str) -> bool:
        if isinstance(error, TelegramForbiddenError):
            logger.warning(
                "⚠️ Пользователь %s недоступен (%s): бот заблокирован",
                user.telegram_id,
                context,
            )
            return True

        if isinstance(error, TelegramBadRequest) and self._is_unreachable_error(error):
            logger.warning(
                "⚠️ Пользователь %s недоступен (%s): %s",
                user.telegram_id,
                context,
                error,
            )
            return True

        return False
    
    async def start_monitoring(self):
        if self.is_running:
            logger.warning("Мониторинг уже запущен")
            return
        
        self.is_running = True
        logger.info("🔄 Запуск службы мониторинга")
        # Start dedicated SLA loop with its own interval for timely 5-min checks
        try:
            if not self._sla_task or self._sla_task.done():
                self._sla_task = asyncio.create_task(self._sla_loop())
        except Exception as e:
            logger.error(f"Не удалось запустить SLA-мониторинг: {e}")
        
        while self.is_running:
            try:
                await self._monitoring_cycle()
                await asyncio.sleep(settings.MONITORING_INTERVAL * 60) 
                
            except Exception as e:
                logger.error(f"Ошибка в цикле мониторинга: {e}")
                await asyncio.sleep(60) 
    
    def stop_monitoring(self):
        self.is_running = False
        logger.info("ℹ️ Мониторинг остановлен")
        try:
            if self._sla_task and not self._sla_task.done():
                self._sla_task.cancel()
        except Exception:
            pass
    
    async def _monitoring_cycle(self):
        async for db in get_db():
            try:
                if settings.IS_ARTEM:
                    logger.info("settings.IS_ARTEM=True")
                    # await self._collect_user_daily_traffic_usage(db)
                    await self._process_android_rate_requests(db)
                    # await self._process_expired_subscription_feedbacks(db)
                    await self._log_monitoring_event(
                        db,
                        "user_daily_traffic_only_cycle_completed",
                        "Цикл мониторинга выполнен только для сбора дневного трафика",
                        {"timestamp": datetime.utcnow().isoformat()},
                    )
                    return

                await self._cleanup_notification_cache()

                expired_offers = await deactivate_expired_offers(db)
                if expired_offers:
                    logger.info(f"🧹 Деактивировано {expired_offers} просроченных скидочных предложений")

                expired_active_discounts = await cleanup_expired_promo_offer_discounts(db)
                if expired_active_discounts:
                    logger.info(
                        "🧹 Сброшено %s активных скидок промо-предложений с истекшим сроком",
                        expired_active_discounts,
                    )

                cleaned_test_access = await promo_offer_service.cleanup_expired_test_access(db)
                if cleaned_test_access:
                    logger.info(f"🧹 Отозвано {cleaned_test_access} истекших тестовых доступов к сквадам")

                await self._check_expired_subscriptions(db)
                await self._check_expiring_subscriptions(db)
                await self._check_trial_expiring_soon(db)
                await self._sync_with_remnawave(db)
                await self._check_trial_inactivity_notifications(db)
                await self._check_trial_channel_subscriptions(db)
                await self._check_expired_subscription_followups(db)
                await self._process_expired_subscription_feedbacks(db)
                if settings.ENABLE_AUTOPAY:
                    await self._process_autopayments(db)
                await self._cleanup_inactive_users(db)
                await self._collect_daily_subscription_metrics(db)
                await self._collect_user_daily_metrics(db)
                await self._collect_trial_expiry_daily_metrics(db)
                await self._collect_user_daily_traffic_usage(db)
                await self._process_android_rate_requests(db)
                await self._cleanup_cabinet_notifications(db)
                
                await self._log_monitoring_event(
                    db, "monitoring_cycle_completed", 
                    "Цикл мониторинга успешно завершен", 
                    {"timestamp": datetime.utcnow().isoformat()}
                )
                
            except Exception as e:
                logger.error(f"Ошибка в цикле мониторинга: {e}")
                await self._log_monitoring_event(
                    db, "monitoring_cycle_error", 
                    f"Ошибка в цикле мониторинга: {str(e)}", 
                    {"error": str(e)},
                    is_success=False
                )
            finally:
                break 
    
    async def _cleanup_notification_cache(self):
        current_time = datetime.utcnow()

        if (current_time - self._last_cleanup).total_seconds() >= 3600:
            old_count = len(self._notified_users)
            self._notified_users.clear()
            # Отметки о неудачном автоплатеже держим до конца периода подписки,
            # иначе дедуп сбрасывался бы каждый час вместе с общим кешем.
            self._autopay_failed_notified = {
                subscription_id: end_date
                for subscription_id, end_date in self._autopay_failed_notified.items()
                if end_date > current_time
            }
            self._last_cleanup = current_time
            logger.info(f"🧹 Очищен кеш уведомлений ({old_count} записей)")

    async def _cleanup_cabinet_notifications(self, db: AsyncSession):
        """Retention-очистка ленты уведомлений кабинета (раз в ~6 часов)."""
        current_time = datetime.utcnow()
        if (current_time - self._last_cabinet_notifications_cleanup).total_seconds() < 6 * 3600:
            return
        self._last_cabinet_notifications_cleanup = current_time

        try:
            from app.database.crud.cabinet_notification import cleanup_old_notifications

            deleted = await cleanup_old_notifications(
                db,
                retention_days=settings.CABINET_NOTIFICATIONS_RETENTION_DAYS,
                max_per_user=settings.CABINET_NOTIFICATIONS_MAX_PER_USER,
            )
            if deleted:
                logger.info(f"🧹 Удалено {deleted} старых уведомлений кабинета")
        except Exception as e:
            logger.error(f"Ошибка очистки уведомлений кабинета: {e}")
    
    async def _check_expired_subscriptions(self, db: AsyncSession):
        try:
            expired_subscriptions = await get_expired_subscriptions(db)
            
            for subscription in expired_subscriptions:
                from app.database.crud.subscription import expire_subscription
                await expire_subscription(db, subscription)
                
                user = await get_user_by_id(db, subscription.user_id)
                if user:
                    await self._notify_cabinet(
                        db,
                        user_id=user.id,
                        type="subscription_expired",
                        payload={
                            "endDate": subscription.end_date.isoformat()
                            if subscription.end_date
                            else None,
                        },
                    )
                if (
                    self.SEND_LEGACY_EXPIRED_TELEGRAM_NOTIFICATIONS
                    and user
                    and self.bot
                ):
                    await self._send_subscription_expired_notification(user)

                logger.info(f"🔴 Подписка пользователя {subscription.user_id} истекла и статус изменен на 'expired'")
            
            if expired_subscriptions:
                await self._log_monitoring_event(
                    db, "expired_subscriptions_processed",
                    f"Обработано {len(expired_subscriptions)} истёкших подписок",
                    {"count": len(expired_subscriptions)}
                )
                
        except Exception as e:
            logger.error(f"Ошибка проверки истёкших подписок: {e}")

    async def update_remnawave_user(
        self,
        db: AsyncSession,
        subscription: Subscription
    ) -> Optional[RemnaWaveUser]:
        
        try:
            user = await get_user_by_id(db, subscription.user_id)
            if not user or not user.remnawave_uuid:
                logger.error(f"RemnaWave UUID не найден для пользователя {subscription.user_id}")
                return None
            
            current_time = datetime.utcnow()
            is_active = (subscription.status == SubscriptionStatus.ACTIVE.value and 
                        subscription.end_date > current_time)
            
            if (subscription.status == SubscriptionStatus.ACTIVE.value and 
                subscription.end_date <= current_time):
                subscription.status = SubscriptionStatus.EXPIRED.value
                await db.commit()
                is_active = False
                logger.info(f"📝 Статус подписки {subscription.id} обновлен на 'expired'")
            
            if not self.subscription_service.is_configured:
                logger.warning(
                    "RemnaWave API не настроен. Пропускаем обновление пользователя %s",
                    subscription.user_id,
                )
                return None

            async with self.subscription_service.get_api_client() as api:
                hwid_limit = resolve_hwid_device_limit_for_payload(subscription)

                update_kwargs = dict(
                    uuid=user.remnawave_uuid,
                    status=UserStatus.ACTIVE if is_active else UserStatus.EXPIRED,
                    expire_at=subscription.end_date,
                    traffic_limit_bytes=self._gb_to_bytes(subscription.traffic_limit_gb),
                    traffic_limit_strategy=TrafficLimitStrategy.MONTH,
                    description=settings.format_remnawave_user_description(
                        full_name=user.full_name,
                        username=user.username,
                        telegram_id=user.telegram_id
                    ),
                    active_internal_squads=subscription.connected_squads,
                )

                if hwid_limit is not None:
                    update_kwargs['hwid_device_limit'] = hwid_limit

                updated_user = await api.update_user(**update_kwargs)
                
                subscription.subscription_url = updated_user.subscription_url
                subscription.subscription_crypto_link = updated_user.happ_crypto_link
                await db.commit()
                
                status_text = "активным" if is_active else "истёкшим"
                logger.info(f"✅ Обновлен RemnaWave пользователь {user.remnawave_uuid} со статусом {status_text}")
                return updated_user
                
        except RemnaWaveAPIError as e:
            logger.error(f"Ошибка обновления RemnaWave пользователя: {e}")
            return None
        except Exception as e:
            logger.error(f"Ошибка обновления RemnaWave пользователя: {e}")
            return None
    
    async def _check_expiring_subscriptions(self, db: AsyncSession):
        try:
            warning_days = settings.get_autopay_warning_days()
            all_processed_users = set() 
            
            for days in warning_days:
                expiring_subscriptions = await self._get_expiring_paid_subscriptions(db, days)
                sent_count = 0
                
                for subscription in expiring_subscriptions:
                    user = await get_user_by_id(db, subscription.user_id)
                    if not user:
                        continue

                    user_key = f"user_{user.telegram_id}_today"

                    if (await notification_sent(db, user.id, subscription.id, "expiring", days) or
                        user_key in all_processed_users):
                        logger.debug(f"🔄 Пропускаем дублирование для пользователя {user.telegram_id} на {days} дней")
                        continue

                    should_send = True
                    for other_days in warning_days:
                        if other_days < days:
                            other_subs = await self._get_expiring_paid_subscriptions(db, other_days)
                            if any(s.user_id == user.id for s in other_subs):
                                should_send = False
                                logger.debug(f"🎯 Пропускаем уведомление на {days} дней для пользователя {user.telegram_id}, есть более срочное на {other_days} дней")
                                break

                    if not should_send:
                        continue

                    if self.bot:
                        success = await self._send_subscription_expiring_notification(user, subscription, days)
                        if success:
                            await record_notification(db, user.id, subscription.id, "expiring", days)
                            await self._notify_cabinet(
                                db,
                                user_id=user.id,
                                type="subscription_expiring",
                                payload={
                                    "days": days,
                                    "endDate": subscription.end_date.isoformat()
                                    if subscription.end_date
                                    else None,
                                    "autopayEnabled": bool(subscription.autopay_enabled),
                                },
                            )
                            all_processed_users.add(user_key)
                            sent_count += 1
                            logger.info(f"✅ Пользователю {user.telegram_id} отправлено уведомление об истечении подписки через {days} дней")
                        else:
                            logger.warning(f"❌ Не удалось отправить уведомление пользователю {user.telegram_id}")
                
                if sent_count > 0:
                    await self._log_monitoring_event(
                        db, "expiring_notifications_sent",
                        f"Отправлено {sent_count} уведомлений об истечении через {days} дней",
                        {"days": days, "count": sent_count}
                    )
                    
        except Exception as e:
            logger.error(f"Ошибка проверки истекающих подписок: {e}")
    
    async def _check_trial_expiring_soon(self, db: AsyncSession):
        try:
            threshold_time = datetime.utcnow() + timedelta(hours=2)

            result = await db.execute(
                select(Subscription)
                .options(
                    selectinload(Subscription.user).selectinload(User.promo_group),
                    selectinload(Subscription.user)
                    .selectinload(User.user_promo_groups)
                    .selectinload(UserPromoGroup.promo_group),
                )
                .where(
                    and_(
                        Subscription.status == SubscriptionStatus.ACTIVE.value,
                        Subscription.is_trial == True,
                        Subscription.end_date <= threshold_time,
                        Subscription.end_date > datetime.utcnow()
                    )
                )
            )
            trial_expiring = result.scalars().all()
            
            for subscription in trial_expiring:
                user = subscription.user
                if not user:
                    continue

                if await notification_sent(db, user.id, subscription.id, "trial_2h"):
                    continue

                if self.bot:
                    success = await self._send_trial_ending_notification(user, subscription)
                    if success:
                        await record_notification(db, user.id, subscription.id, "trial_2h")
                        await self._notify_cabinet(
                            db,
                            user_id=user.id,
                            type="trial_ending",
                            payload={
                                "endDate": subscription.end_date.isoformat()
                                if subscription.end_date
                                else None,
                            },
                        )
                        logger.info(f"🎁 Пользователю {user.telegram_id} отправлено уведомление об окончании тестовой подписки через 2 часа")
            
            if trial_expiring:
                await self._log_monitoring_event(
                    db, "trial_expiring_notifications_sent",
                    f"Отправлено {len(trial_expiring)} уведомлений об окончании тестовых подписок",
                    {"count": len(trial_expiring)}
                )
                
        except Exception as e:
            logger.error(f"Ошибка проверки истекающих тестовых подписок: {e}")

    async def _check_trial_inactivity_notifications(self, db: AsyncSession):
        if not NotificationSettingsService.are_notifications_globally_enabled():
            return
        if not self.bot:
            return

        try:
            now = datetime.utcnow()
            one_hour_ago = now - timedelta(hours=1)

            result = await db.execute(
                select(Subscription)
                .options(selectinload(Subscription.user))
                .where(
                    and_(
                        Subscription.status == SubscriptionStatus.ACTIVE.value,
                        Subscription.is_trial == True,
                        Subscription.start_date.isnot(None),
                        Subscription.start_date <= one_hour_ago,
                        Subscription.end_date > now,
                    )
                )
            )

            subscriptions = result.scalars().all()
            sent_1h = 0
            sent_24h = 0

            for subscription in subscriptions:
                user = subscription.user
                if not user:
                    continue

                if user.has_connected_to_vpn:
                    continue

                start_date = subscription.start_date
                if not start_date:
                    continue

                time_since_start = now - start_date

                if (NotificationSettingsService.is_trial_inactive_1h_enabled()
                        and timedelta(hours=1) <= time_since_start < timedelta(hours=24)):
                    if not await notification_sent(db, user.id, subscription.id, "trial_inactive_1h"):
                        success = await self._send_trial_inactive_notification(user, subscription, 1)
                        if success:
                            await record_notification(db, user.id, subscription.id, "trial_inactive_1h")
                            sent_1h += 1

                if NotificationSettingsService.is_trial_inactive_24h_enabled() and time_since_start >= timedelta(hours=24):
                    if not await notification_sent(db, user.id, subscription.id, "trial_inactive_24h"):
                        success = await self._send_trial_inactive_notification(user, subscription, 24)
                        if success:
                            await record_notification(db, user.id, subscription.id, "trial_inactive_24h")
                            sent_24h += 1

            if sent_1h or sent_24h:
                await self._log_monitoring_event(
                    db,
                    "trial_inactivity_notifications",
                    f"Отправлено {sent_1h} уведомлений спустя 1 час и {sent_24h} спустя 24 часа",
                    {"sent_1h": sent_1h, "sent_24h": sent_24h},
                )

        except Exception as e:
            logger.error(f"Ошибка проверки неактивных тестовых подписок: {e}")

    async def _check_trial_channel_subscriptions(self, db: AsyncSession):
        if not settings.CHANNEL_IS_REQUIRED_SUB:
            return

        if not settings.CHANNEL_DISABLE_TRIAL_ON_UNSUBSCRIBE:
            logger.debug(
                "ℹ️ Проверка отписок от канала отключена — деактивация триальных подписок не требуется"
            )
            return

        channel_id = settings.CHANNEL_SUB_ID
        if not channel_id:
            return

        if not self.bot:
            logger.debug("⚠️ Пропускаем проверку подписки на канал — бот недоступен")
            return

        try:
            now = datetime.utcnow()
            notifications_allowed = (
                NotificationSettingsService.are_notifications_globally_enabled()
                and NotificationSettingsService.is_trial_channel_unsubscribed_enabled()
            )
            result = await db.execute(
                select(Subscription)
                .options(selectinload(Subscription.user))
                .where(
                    and_(
                        Subscription.is_trial.is_(True),
                        Subscription.end_date > now,
                        Subscription.status.in_(
                            [
                                SubscriptionStatus.ACTIVE.value,
                                SubscriptionStatus.DISABLED.value,
                            ]
                        ),
                    )
                )
            )

            subscriptions = result.scalars().all()
            if not subscriptions:
                return

            disabled_count = 0
            restored_count = 0

            for subscription in subscriptions:
                user = subscription.user
                if not user or not user.telegram_id:
                    continue

                try:
                    member = await self.bot.get_chat_member(channel_id, user.telegram_id)
                    member_status = member.status
                    is_member = member_status in (
                        ChatMemberStatus.MEMBER,
                        ChatMemberStatus.ADMINISTRATOR,
                        ChatMemberStatus.CREATOR,
                    )
                except TelegramForbiddenError as error:
                    logger.error(
                        "❌ Не удалось проверить подписку пользователя %s на канал %s: бот заблокирован (%s)",
                        user.telegram_id,
                        channel_id,
                        error,
                    )
                    continue
                except TelegramBadRequest as error:
                    logger.error(
                        "❌ Ошибка Telegram при проверке подписки пользователя %s: %s",
                        user.telegram_id,
                        error,
                    )
                    continue
                except Exception as error:
                    logger.error(
                        "❌ Неожиданная ошибка при проверке подписки пользователя %s: %s",
                        user.telegram_id,
                        error,
                    )
                    continue

                if (
                    subscription.status == SubscriptionStatus.ACTIVE.value
                    and subscription.is_trial
                    and not is_member
                ):
                    subscription = await deactivate_subscription(db, subscription)
                    disabled_count += 1
                    logger.info(
                        "🚫 Триальная подписка пользователя %s (ID %s) отключена из-за отписки от канала",
                        user.telegram_id,
                        subscription.id,
                    )

                    if user.remnawave_uuid:
                        try:
                            await self.subscription_service.disable_remnawave_user(user.remnawave_uuid)
                        except Exception as api_error:
                            logger.error(
                                "❌ Не удалось отключить пользователя RemnaWave %s: %s",
                                user.remnawave_uuid,
                                api_error,
                            )

                    if notifications_allowed:
                        if not await notification_sent(
                            db,
                            user.id,
                            subscription.id,
                            "trial_channel_unsubscribed",
                        ):
                            sent = await self._send_trial_channel_unsubscribed_notification(user)
                            if sent:
                                await record_notification(
                                    db,
                                    user.id,
                                    subscription.id,
                                    "trial_channel_unsubscribed",
                                )
                elif (
                    subscription.status == SubscriptionStatus.DISABLED.value
                    and subscription.is_trial
                    and is_member
                ):
                    subscription.status = SubscriptionStatus.ACTIVE.value
                    subscription.updated_at = datetime.utcnow()
                    await db.commit()
                    await db.refresh(subscription)
                    restored_count += 1

                    logger.info(
                        "✅ Триальная подписка пользователя %s (ID %s) восстановлена после повторной подписки на канал",
                        user.telegram_id,
                        subscription.id,
                    )

                    try:
                        if user.remnawave_uuid:
                            await self.subscription_service.update_remnawave_user(db, subscription)
                        else:
                            await self.subscription_service.create_remnawave_user(db, subscription)
                    except Exception as api_error:
                        logger.error(
                            "❌ Не удалось обновить RemnaWave пользователя %s: %s",
                            user.telegram_id,
                            api_error,
                        )

                    await clear_notification_by_type(
                        db,
                        subscription.id,
                        "trial_channel_unsubscribed",
                    )

            if disabled_count or restored_count:
                await self._log_monitoring_event(
                    db,
                    "trial_channel_subscription_check",
                    (
                        "Проверено {total} триальных подписок: отключено {disabled}, "
                        "восстановлено {restored}"
                    ).format(
                        total=len(subscriptions),
                        disabled=disabled_count,
                        restored=restored_count,
                    ),
                    {
                        "checked": len(subscriptions),
                        "disabled": disabled_count,
                        "restored": restored_count,
                    },
                )

        except Exception as error:
            logger.error(f"Ошибка проверки подписки на канал для триальных пользователей: {error}")

    async def _check_expired_subscription_followups(self, db: AsyncSession):
        if not NotificationSettingsService.are_notifications_globally_enabled():
            return
        if not self.SEND_LEGACY_EXPIRED_FOLLOWUPS:
            return
        if not self.bot:
            return

        try:
            now = datetime.utcnow()

            result = await db.execute(
                select(Subscription)
                .options(selectinload(Subscription.user))
                .where(
                    and_(
                        Subscription.is_trial == False,
                        Subscription.end_date <= now,
                    )
                )
            )

            subscriptions = result.scalars().all()
            sent_day1 = 0
            sent_wave2 = 0
            sent_wave3 = 0

            for subscription in subscriptions:
                user = subscription.user
                if not user:
                    continue

                if subscription.end_date is None:
                    continue

                time_since_end = now - subscription.end_date
                if time_since_end.total_seconds() < 0:
                    continue

                days_since = time_since_end.total_seconds() / 86400

                # Day 1 reminder
                if NotificationSettingsService.is_expired_1d_enabled() and 1 <= days_since < 2:
                    if not await notification_sent(db, user.id, subscription.id, "expired_1d"):
                        success = await self._send_expired_day1_notification(user, subscription)
                        if success:
                            await record_notification(db, user.id, subscription.id, "expired_1d")
                            sent_day1 += 1

                # Second wave (2-3 days) discount
                if NotificationSettingsService.is_second_wave_enabled() and 2 <= days_since < 4:
                    if not await notification_sent(db, user.id, subscription.id, "expired_discount_wave2"):
                        percent = NotificationSettingsService.get_second_wave_discount_percent()
                        valid_hours = NotificationSettingsService.get_second_wave_valid_hours()
                        offer = await upsert_discount_offer(
                            db,
                            user_id=user.id,
                            subscription_id=subscription.id,
                            notification_type="expired_discount_wave2",
                            discount_percent=percent,
                            bonus_amount_kopeks=0,
                            valid_hours=valid_hours,
                            effect_type="percent_discount",
                        )
                        success = await self._send_expired_discount_notification(
                            user,
                            subscription,
                            percent,
                            offer.expires_at,
                            offer.id,
                            "second",
                        )
                        if success:
                            await record_notification(db, user.id, subscription.id, "expired_discount_wave2")
                            sent_wave2 += 1

                # Third wave (N days) discount
                if NotificationSettingsService.is_third_wave_enabled():
                    trigger_days = NotificationSettingsService.get_third_wave_trigger_days()
                    if trigger_days <= days_since < trigger_days + 1:
                        if not await notification_sent(db, user.id, subscription.id, "expired_discount_wave3"):
                            percent = NotificationSettingsService.get_third_wave_discount_percent()
                            valid_hours = NotificationSettingsService.get_third_wave_valid_hours()
                            offer = await upsert_discount_offer(
                                db,
                                user_id=user.id,
                                subscription_id=subscription.id,
                                notification_type="expired_discount_wave3",
                                discount_percent=percent,
                                bonus_amount_kopeks=0,
                                valid_hours=valid_hours,
                                effect_type="percent_discount",
                            )
                            success = await self._send_expired_discount_notification(
                                user,
                                subscription,
                                percent,
                                offer.expires_at,
                                offer.id,
                                "third",
                                trigger_days=trigger_days,
                            )
                            if success:
                                await record_notification(db, user.id, subscription.id, "expired_discount_wave3")
                                sent_wave3 += 1

            if sent_day1 or sent_wave2 or sent_wave3:
                await self._log_monitoring_event(
                    db,
                    "expired_followups_sent",
                    (
                        "Follow-ups: 1д={0}, скидка 2-3д={1}, скидка N={2}".format(
                            sent_day1,
                            sent_wave2,
                            sent_wave3,
                        )
                    ),
                    {
                        "day1": sent_day1,
                        "wave2": sent_wave2,
                        "wave3": sent_wave3,
                    },
                )

        except Exception as e:
            logger.error(f"Ошибка проверки напоминаний об истекшей подписке: {e}")

    async def _get_expiring_paid_subscriptions(self, db: AsyncSession, days_before: int) -> List[Subscription]:
        current_time = datetime.utcnow()
        threshold_date = current_time + timedelta(days=days_before)
        
        result = await db.execute(
            select(Subscription)
            .options(selectinload(Subscription.user))
            .where(
                and_(
                    Subscription.status == SubscriptionStatus.ACTIVE.value,
                    Subscription.is_trial == False, 
                    Subscription.end_date > current_time,
                    Subscription.end_date <= threshold_date
                )
            )
        )
        
        logger.debug(f"🔍 Поиск платных подписок, истекающих в ближайшие {days_before} дней")
        logger.debug(f"📅 Текущее время: {current_time}")
        logger.debug(f"📅 Пороговая дата: {threshold_date}")
        
        subscriptions = result.scalars().all()
        logger.info(f"📊 Найдено {len(subscriptions)} платных подписок для уведомлений")
        
        return subscriptions
    
    @staticmethod
    def _get_user_promo_offer_discount_percent(user: Optional[User]) -> int:
        if not user:
            return 0

        try:
            percent = int(getattr(user, "promo_offer_discount_percent", 0) or 0)
        except (TypeError, ValueError):
            return 0

        expires_at = getattr(user, "promo_offer_discount_expires_at", None)
        if expires_at and expires_at <= datetime.utcnow():
            return 0

        return max(0, min(100, percent))

    @staticmethod
    async def _consume_user_promo_offer_discount(db: AsyncSession, user: User) -> None:
        percent = MonitoringService._get_user_promo_offer_discount_percent(user)
        if percent <= 0:
            return

        source = getattr(user, "promo_offer_discount_source", None)
        log_payload = {
            "offer_id": None,
            "percent": percent,
            "source": source,
            "effect_type": None,
        }

        try:
            offer = await get_latest_claimed_offer_for_user(db, user.id, source)
        except Exception as lookup_error:  # pragma: no cover - defensive logging
            logger.warning(
                "Failed to resolve latest claimed promo offer for user %s: %s",
                user.id,
                lookup_error,
            )
            offer = None

        if offer:
            log_payload["offer_id"] = offer.id
            log_payload["effect_type"] = offer.effect_type
            if not log_payload["percent"] and offer.discount_percent:
                log_payload["percent"] = offer.discount_percent

        user.promo_offer_discount_percent = 0
        user.promo_offer_discount_source = None
        user.promo_offer_discount_expires_at = None
        user.updated_at = datetime.utcnow()

        await db.commit()
        await db.refresh(user)

        try:
            await log_promo_offer_action(
                db,
                user_id=user.id,
                offer_id=log_payload.get("offer_id"),
                action="consumed",
                source=log_payload.get("source"),
                percent=log_payload.get("percent"),
                effect_type=log_payload.get("effect_type"),
                details={"reason": "autopay_consumed"},
            )
        except Exception as log_error:  # pragma: no cover - defensive logging
            logger.warning(
                "Failed to record promo offer autopay log for user %s: %s",
                user.id,
                log_error,
            )
            try:
                await db.rollback()
            except Exception as rollback_error:  # pragma: no cover - defensive logging
                logger.warning(
                    "Failed to rollback session after promo offer autopay log failure: %s",
                    rollback_error,
                )

    async def _resolve_tariff_autopay_charge(
        self,
        db: AsyncSession,
        subscription: Subscription,
        user: User,
    ) -> Optional[Tuple[Any, int, int]]:
        """(plan, period_days, price_kopeks) для тарифной подписки, либо None.

        Цена тарифа абсолютная и зависит от связки (план, период, ценовая когорта
        пользователя), поэтому legacy-расчёт calculate_renewal_price
        (PERIOD_PRICES + серверы/устройства/трафик) для таких подписок неприменим.
        Продлеваем ровно на тот период, который пользователь купил.
        """
        from app.services.plan_pricing_service import (
            get_plan_by_id,
            get_plan_price,
            resolve_pricing_cohort,
        )

        period_days = subscription.plan_period_days or 0
        if period_days <= 0:
            logger.warning(
                "💳 Автоплатеж пропущен: у тарифной подписки %s не задан plan_period_days",
                subscription.id,
            )
            return None

        plan = subscription.plan or await get_plan_by_id(db, subscription.plan_id)
        if plan is None:
            logger.warning(
                "💳 Автоплатеж пропущен: тариф %s подписки %s не найден",
                subscription.plan_id,
                subscription.id,
            )
            return None

        price_kopeks = await get_plan_price(
            db,
            plan.id,
            period_days,
            cohort=resolve_pricing_cohort(user),
        )
        if price_kopeks is None:
            logger.warning(
                "💳 Автоплатеж пропущен: для тарифа %s нет цены на период %s дн. (подписка %s)",
                plan.code,
                period_days,
                subscription.id,
            )
            return None

        return plan, period_days, int(price_kopeks)

    async def _process_autopayments(self, db: AsyncSession):
        try:
            current_time = datetime.utcnow()
            
            result = await db.execute(
                select(Subscription)
                .options(
                    selectinload(Subscription.user).options(
                        selectinload(User.promo_group),
                        selectinload(User.user_promo_groups).selectinload(UserPromoGroup.promo_group),
                    )
                )
                .where(
                    and_(
                        Subscription.status == SubscriptionStatus.ACTIVE.value,
                        Subscription.autopay_enabled == True,
                        Subscription.is_trial == False
                    )
                )
            )
            all_autopay_subscriptions = result.scalars().all()
            
            autopay_subscriptions = []
            for sub in all_autopay_subscriptions:
                days_before_expiry = (sub.end_date - current_time).days
                if days_before_expiry <= min(sub.autopay_days_before, 3):
                    autopay_subscriptions.append(sub)
            
            processed_count = 0
            failed_count = 0
            skipped_count = 0

            for subscription in autopay_subscriptions:
                user = subscription.user
                if not user:
                    continue

                autopay_key = f"autopay_{user.telegram_id}_{subscription.id}"
                if autopay_key in self._notified_users:
                    continue

                is_tariff = not subscription.is_legacy
                plan = None
                promo_discount_percent = 0
                promo_discount_value = 0

                if is_tariff:
                    # Тарифная подписка: цена и период берутся из тарифа, как в
                    # ручном продлении. Процентная промо-скидка к тарифам не
                    # применяется — там работают только офферы с фикс. ценой,
                    # а их активация требует действия пользователя.
                    tariff_charge = await self._resolve_tariff_autopay_charge(db, subscription, user)
                    if tariff_charge is None:
                        skipped_count += 1
                        continue
                    plan, period_days, charge_amount = tariff_charge
                else:
                    # Legacy à-la-carte подписка: пересчет по текущим ценам
                    # (период + серверы + устройства + трафик).
                    period_days = 30
                    renewal_cost = await self.subscription_service.calculate_renewal_price(
                        subscription, period_days, db, user=user
                    )
                    promo_discount_percent = self._get_user_promo_offer_discount_percent(user)
                    charge_amount = renewal_cost

                    if renewal_cost > 0 and promo_discount_percent > 0:
                        charge_amount, promo_discount_value = apply_percentage_discount(
                            renewal_cost,
                            promo_discount_percent,
                        )

                if user.balance_kopeks < charge_amount:
                    failed_count += 1
                    await self._notify_autopay_failed(db, user, subscription, charge_amount)
                    logger.warning(f"💳 Недостаточно средств для автопродления у пользователя {user.telegram_id}")
                    continue

                old_end_date = subscription.end_date
                transaction_id = None

                try:
                    if is_tariff:
                        # Списание + продление + транзакция + пересинк Remnawave
                        # одним куском — тот же путь, что и у ручного продления.
                        from app.handlers.subscription.tariffs import finalize_tariff_renewal

                        renewal = await finalize_tariff_renewal(
                            db, user, subscription, plan, period_days, charge_amount
                        )
                        success = renewal is not None
                        if success:
                            subscription, transaction, old_end_date = renewal
                            transaction_id = transaction.id
                    else:
                        success = await subtract_user_balance(
                            db, user, charge_amount,
                            "Автопродление подписки"
                        )
                        if success:
                            await extend_subscription(db, subscription, period_days)
                except Exception as charge_error:
                    success = False
                    logger.error(
                        "💳 Ошибка автопродления подписки %s пользователя %s: %s",
                        subscription.id,
                        user.telegram_id,
                        charge_error,
                        exc_info=True,
                    )

                if not success:
                    failed_count += 1
                    await self._notify_autopay_failed(db, user, subscription, charge_amount)
                    logger.warning(f"💳 Ошибка списания средств для автопродления пользователя {user.telegram_id}")
                    continue

                await record_subscription_renewal_event(
                    db,
                    user_id=user.id,
                    subscription_id=subscription.id,
                    transaction_id=transaction_id,
                    amount_kopeks=charge_amount,
                    period_days=period_days,
                    previous_end_date=old_end_date,
                    new_end_date=subscription.end_date,
                    balance_after=user.balance_kopeks,
                    source="autopay",
                )

                if not is_tariff:
                    await self.subscription_service.update_remnawave_user(
                        db,
                        subscription,
                        reset_traffic=settings.RESET_TRAFFIC_ON_PAYMENT,
                        reset_reason="автопродление подписки",
                    )

                if promo_discount_value > 0:
                    await self._consume_user_promo_offer_discount(db, user)

                self._autopay_failed_notified.pop(subscription.id, None)

                if self.bot and settings.AUTOPAY_NOTIFY_SUCCESS:
                    await self._send_autopay_success_notification(user, charge_amount, period_days)

                processed_count += 1
                self._notified_users.add(autopay_key)
                logger.info(
                    "💳 Автопродление подписки пользователя %s успешно: %s дн., списано %s (скидка %s%%)",
                    user.telegram_id,
                    period_days,
                    charge_amount,
                    promo_discount_percent,
                )

            if processed_count > 0 or failed_count > 0 or skipped_count > 0:
                await self._log_monitoring_event(
                    db, "autopayments_processed",
                    f"Автоплатежи: успешно {processed_count}, неудачно {failed_count}, пропущено {skipped_count}",
                    {
                        "processed": processed_count,
                        "failed": failed_count,
                        "skipped": skipped_count,
                    }
                )
                
        except Exception as e:
            logger.error(f"Ошибка обработки автоплатежей: {e}")
    
    async def _notify_cabinet(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        type: str,
        payload: Optional[dict] = None,
    ) -> None:
        """Дублирует событие мониторинга в ленту уведомлений кабинета (best-effort)."""
        try:
            from app.services.cabinet_notification_service import notify

            await notify(db, user_id=user_id, type=type, payload=payload)
        except Exception as exc:
            logger.warning(
                "Не удалось создать уведомление кабинета (%s) для пользователя %s: %s",
                type,
                user_id,
                exc,
            )

    async def _notify_autopay_failed(
        self,
        db: AsyncSession,
        user: User,
        subscription: Subscription,
        charge_amount: int,
    ) -> None:
        """Сообщение в бот + запись в ленту кабинета о неудачном автоплатеже."""
        if (
            self.bot
            and settings.AUTOPAY_NOTIFY_FAILED
            and self._mark_autopay_failure_notified(subscription)
        ):
            await self._send_autopay_failed_notification(
                user, user.balance_kopeks, charge_amount
            )

        await self._notify_cabinet_autopay_failed(db, user, subscription, charge_amount)

    def _mark_autopay_failure_notified(self, subscription: Subscription) -> bool:
        """True, если по этому периоду подписки сообщение еще не отправлялось.

        Неудачная ветка автоплатежа выполняется каждый цикл мониторинга, пока
        подписка находится в окне списания (до 3 суток), поэтому без дедупа
        пользователь получил бы десятки одинаковых сообщений. Ключом служит
        end_date: после продления он меняется, и следующий период снова
        уведомляется.
        """
        if self._autopay_failed_notified.get(subscription.id) == subscription.end_date:
            return False

        self._autopay_failed_notified[subscription.id] = subscription.end_date
        return True

    async def _notify_cabinet_autopay_failed(
        self,
        db: AsyncSession,
        user: User,
        subscription: Subscription,
        charge_amount: int,
    ) -> None:
        # Ветки неудачи автоплатежа выполняются каждый цикл мониторинга —
        # дедупим ленту кабинета через кеш (очищается раз в час).
        cabinet_key = f"autopay_failed_cabinet_{user.id}_{subscription.id}"
        if cabinet_key in self._notified_users:
            return
        self._notified_users.add(cabinet_key)
        await self._notify_cabinet(
            db,
            user_id=user.id,
            type="autopay_failed",
            payload={
                "balanceKopeks": user.balance_kopeks,
                "requiredKopeks": charge_amount,
            },
        )

    async def _send_subscription_expired_notification(self, user: User) -> bool:
        try:
            message = """
⛔ <b>Подписка истекла</b>

Ваша подписка истекла. Для восстановления доступа продлите подписку.

🔧 Доступ к серверам заблокирован до продления.
"""
            
            from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [build_miniapp_or_callback_button(text="💎 Купить подписку", callback_data="menu_buy")],
                [build_miniapp_or_callback_button(text="💳 Пополнить баланс", callback_data="balance_topup")],
            ])

            await self._send_message_with_logo(
                chat_id=user.telegram_id,
                text=message,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            return True

        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            if self._handle_unreachable_user(user, exc, "уведомление об истечении подписки"):
                return True
            logger.error(
                "Ошибка Telegram API при отправке уведомления об истечении подписки пользователю %s: %s",
                user.telegram_id,
                exc,
            )
            return False
        except Exception as e:
            logger.error(
                "Ошибка отправки уведомления об истечении подписки пользователю %s: %s",
                user.telegram_id,
                e,
            )
            return False
    
    async def _send_subscription_expiring_notification(self, user: User, subscription: Subscription, days: int) -> bool:
        try:
            from app.utils.formatters import format_days_declension
            
            texts = get_texts(user.language)
            days_text = format_days_declension(days, user.language)
            
            if settings.ENABLE_AUTOPAY:
                if subscription.autopay_enabled:
                    autopay_status = "✅ Включен - подписка продлится автоматически"
                    action_text = f"💰 Убедитесь, что на балансе достаточно средств: {texts.format_price(user.balance_kopeks)}"
                else:
                    autopay_status = "Отключен - не забудьте продлить вручную!"
                    action_text = "💡 Включите автоплатеж или продлите подписку вручную"
            else:
                autopay_status = "Отключен - не забудьте продлить вручную!"
                action_text = "💡 Продлите подписку вручную"
            
            message = f"""
❗️ <b>Подписка истекает через {days_text}!</b>

Ваша платная подписка истекает {format_local_datetime(subscription.end_date, "%d.%m.%Y %H:%M")}.

🔄 <b>Автоплатеж:</b> {autopay_status}

{action_text}
"""
            
            from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [build_miniapp_or_callback_button(
                    text="⏰ Продлить подписку",
                    callback_data="subscription_extend",
                    style=ButtonStyle.SUCCESS,
                )],
                [build_miniapp_or_callback_button(text="💳 Пополнить баланс", callback_data="balance_topup")],
                [build_miniapp_or_callback_button(text="📱 Моя подписка", callback_data="subscription")],
            ])

            await self._send_message_with_image(
                chat_id=user.telegram_id,
                text=message,
                image_path=SUBSCRIPTION_EXPIRING_IMAGE,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            return True

        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            if self._handle_unreachable_user(user, exc, "уведомление об истекающей подписке"):
                return True
            logger.error(
                "Ошибка Telegram API при отправке уведомления об истечении подписки пользователю %s: %s",
                user.telegram_id,
                exc,
            )
            return False
        except Exception as e:
            logger.error(
                "Ошибка отправки уведомления об истечении подписки пользователю %s: %s",
                user.telegram_id,
                e,
            )
            return False
    
    async def _send_trial_ending_notification(self, user: User, subscription: Subscription) -> bool:
        try:
            message = (
                "⏳ <b>Тест заканчивается через 2 часа</b>\n\n"
                "Понравилось? Останься с VPN — выбери тариф и продолжай без перерыва."
            )
            
            from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [build_miniapp_or_callback_button(text="💎 Выбрать тариф", callback_data="menu_buy")],
                [InlineKeyboardButton(text="🆘 Поддержка", callback_data="menu_support")],
            ])

            await self._send_message_with_logo(
                chat_id=user.telegram_id,
                text=message,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            return True

        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            if self._handle_unreachable_user(user, exc, "уведомление о завершении тестовой подписки"):
                return True
            logger.error(
                "Ошибка Telegram API при отправке уведомления о завершении тестовой подписки пользователю %s: %s",
                user.telegram_id,
                exc,
            )
            return False
        except Exception as e:
            logger.error(
                "Ошибка отправки уведомления об окончании тестовой подписки пользователю %s: %s",
                user.telegram_id,
                e,
            )
            return False

    async def _send_trial_inactive_notification(self, user: User, subscription: Subscription, hours: int) -> bool:
        try:
            if hours >= 24:
                message = (
                    "⏳ <b>Твой тест уходит впустую</b>\n\n"
                    "Сутки прошли, а VPN так и не подключён. Давай исправим —\n"
                    "это пара минут."
                )
            else:
                message = (
                    "👋 <b>Застрял на подключении?</b>\n\n"
                    "Доступ уже активен — покажем, как подключиться за минуту."
                )

            from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📲 Подключиться", callback_data="subscription_connect")],
                [InlineKeyboardButton(text="🆘 Поддержка", callback_data="menu_support")],
            ])

            await self._send_message_with_logo(
                chat_id=user.telegram_id,
                text=message,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            return True

        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            if self._handle_unreachable_user(user, exc, "уведомление о бездействии на тесте"):
                return True
            logger.error(
                "Ошибка Telegram API при отправке уведомления об отсутствии подключения пользователю %s: %s",
                user.telegram_id,
                exc,
            )
            return False
        except Exception as e:
            logger.error(
                "Ошибка отправки уведомления об отсутствии подключения пользователю %s: %s",
                user.telegram_id,
                e,
            )
            return False

    async def _send_trial_channel_unsubscribed_notification(self, user: User) -> bool:
        try:
            texts = get_texts(user.language)
            template = texts.get(
                "TRIAL_CHANNEL_UNSUBSCRIBED",
                (
                    "🚫 <b>Доступ приостановлен</b>\n\n"
                    "Мы не нашли вашу подписку на наш канал, поэтому тестовая подписка отключена.\n\n"
                    "Подпишитесь на канал и нажмите «{check_button}», чтобы вернуть доступ."
                ),
            )

            check_button = texts.t("CHANNEL_CHECK_BUTTON", "✅ Я подписался")
            message = template.format(check_button=check_button)

            from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

            buttons = []
            if settings.CHANNEL_LINK:
                buttons.append(
                    [
                        InlineKeyboardButton(
                            text=texts.t("CHANNEL_SUBSCRIBE_BUTTON", "🔗 Подписаться"),
                            url=settings.CHANNEL_LINK,
                        )
                    ]
                )
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=check_button,
                        callback_data="sub_channel_check",
                    )
                ]
            )

            keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

            await self._send_message_with_logo(
                chat_id=user.telegram_id,
                text=message,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            return True

        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            if self._handle_unreachable_user(user, exc, "уведомление об отписке от канала"):
                return True
            logger.error(
                "Ошибка Telegram API при отправке уведомления об отписке от канала пользователю %s: %s",
                user.telegram_id,
                exc,
            )
            return False
        except Exception as error:
            logger.error(
                "Ошибка отправки уведомления об отписке от канала пользователю %s: %s",
                user.telegram_id,
                error,
            )
            return False

    async def _send_expired_day1_notification(self, user: User, subscription: Subscription) -> bool:
        try:
            texts = get_texts(user.language)
            template = texts.get(
                "SUBSCRIPTION_EXPIRED_1D",
                (
                    "⛔ <b>Подписка закончилась</b>\n\n"
                    "Доступ был отключён {end_date}. Продлите подписку, чтобы вернуться в сервис."
                ),
            )
            message = template.format(
                end_date=format_local_datetime(subscription.end_date, "%d.%m.%Y %H:%M"),
                price=settings.format_price(settings.PRICE_30_DAYS),
            )

            from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [build_miniapp_or_callback_button(
                    text=texts.t("SUBSCRIPTION_EXTEND", "💎 Продлить подписку"),
                    callback_data="subscription_extend",
                )],
                [build_miniapp_or_callback_button(
                    text=texts.t("BALANCE_TOPUP", "💳 Пополнить баланс"),
                    callback_data="balance_topup",
                )],
                [InlineKeyboardButton(text=texts.t("SUPPORT_BUTTON", "🆘 Поддержка"), callback_data="menu_support")],
            ])

            await self._send_message_with_logo(
                chat_id=user.telegram_id,
                text=message,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            return True

        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            if self._handle_unreachable_user(user, exc, "напоминание об истекшей подписке"):
                return True
            logger.error(
                "Ошибка Telegram API при отправке напоминания об истекшей подписке пользователю %s: %s",
                user.telegram_id,
                exc,
            )
            return False
        except Exception as e:
            logger.error(
                "Ошибка отправки напоминания об истекшей подписке пользователю %s: %s",
                user.telegram_id,
                e,
            )
            return False

    async def _send_expired_discount_notification(
        self,
        user: User,
        subscription: Subscription,
        percent: int,
        expires_at: datetime,
        offer_id: int,
        wave: str,
        trigger_days: int = None,
    ) -> bool:
        try:
            texts = get_texts(user.language)

            if wave == "second":
                template = texts.get(
                    "SUBSCRIPTION_EXPIRED_SECOND_WAVE",
                    (
                        "🔥 <b>Скидка {percent}% на продление</b>\n\n"
                        "Активируйте предложение, чтобы получить дополнительную скидку. "
                        "Она суммируется с вашей промогруппой и действует до {expires_at}."
                    ),
                )
            else:
                template = texts.get(
                    "SUBSCRIPTION_EXPIRED_THIRD_WAVE",
                    (
                        "🎁 <b>Индивидуальная скидка {percent}%</b>\n\n"
                        "Прошло {trigger_days} дней без подписки — возвращайтесь и активируйте дополнительную скидку. "
                        "Она суммируется с промогруппой и действует до {expires_at}."
                    ),
                )

            message = template.format(
                percent=percent,
                expires_at=format_local_datetime(expires_at, "%d.%m.%Y %H:%M"),
                trigger_days=trigger_days or "",
            )

            from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [build_miniapp_or_callback_button(text="🎁 Получить скидку", callback_data=f"claim_discount_{offer_id}")],
                [build_miniapp_or_callback_button(
                    text=texts.t("SUBSCRIPTION_EXTEND", "💎 Продлить подписку"),
                    callback_data="subscription_extend",
                )],
                [build_miniapp_or_callback_button(
                    text=texts.t("BALANCE_TOPUP", "💳 Пополнить баланс"),
                    callback_data="balance_topup",
                )],
                [InlineKeyboardButton(text=texts.t("SUPPORT_BUTTON", "🆘 Поддержка"), callback_data="menu_support")],
            ])

            await self._send_message_with_logo(
                chat_id=user.telegram_id,
                text=message,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            return True

        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            if self._handle_unreachable_user(user, exc, "скидочное уведомление"):
                return True
            logger.error(
                "Ошибка Telegram API при отправке скидочного уведомления пользователю %s: %s",
                user.telegram_id,
                exc,
            )
            return False
        except Exception as e:
            logger.error(
                "Ошибка отправки скидочного уведомления пользователю %s: %s",
                user.telegram_id,
                e,
            )
            return False

    async def _send_autopay_success_notification(self, user: User, amount: int, days: int):
        try:
            texts = get_texts(user.language)
            message = texts.AUTOPAY_SUCCESS.format(
                days=days,
                amount=settings.format_price(amount)
            )
            await self._send_message_with_logo(
                chat_id=user.telegram_id,
                text=message,
                parse_mode="HTML",
            )
        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            if not self._handle_unreachable_user(user, exc, "уведомление об успешном автоплатеже"):
                logger.error(
                    "Ошибка Telegram API при отправке уведомления об автоплатеже пользователю %s: %s",
                    user.telegram_id,
                    exc,
                )
        except Exception as e:
            logger.error(
                "Ошибка отправки уведомления об автоплатеже пользователю %s: %s",
                user.telegram_id,
                e,
            )

    async def _send_autopay_failed_notification(self, user: User, balance: int, required: int):
        try:
            texts = get_texts(user.language)
            message = texts.AUTOPAY_FAILED.format(
                balance=settings.format_price(balance),
                required=settings.format_price(required)
            )
            
            from aiogram.types import InlineKeyboardMarkup

            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [build_miniapp_or_callback_button(text="💳 Пополнить баланс", callback_data="balance_topup")],
                [build_miniapp_or_callback_button(text="📱 Моя подписка", callback_data="subscription")],
            ])
            
            await self._send_message_with_logo(
                chat_id=user.telegram_id,
                text=message,
                parse_mode="HTML",
                reply_markup=keyboard,
            )

        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            if not self._handle_unreachable_user(user, exc, "уведомление о неудачном автоплатеже"):
                logger.error(
                    "Ошибка Telegram API при отправке уведомления о неудачном автоплатеже пользователю %s: %s",
                    user.telegram_id,
                    exc,
                )
        except Exception as e:
            logger.error(
                "Ошибка отправки уведомления о неудачном автоплатеже пользователю %s: %s",
                user.telegram_id,
                e,
            )
    
    async def _cleanup_inactive_users(self, db: AsyncSession):
        try:
            now = datetime.utcnow()
            if now.hour != 3: 
                return
            
            inactive_users = await get_inactive_users(db, settings.INACTIVE_USER_DELETE_MONTHS)
            deleted_count = 0
            
            for user in inactive_users:
                if not user.subscription or not user.subscription.is_active:
                    success = await delete_user(db, user)
                    if success:
                        deleted_count += 1
            
            if deleted_count > 0:
                await self._log_monitoring_event(
                    db, "inactive_users_cleanup",
                    f"Удалено {deleted_count} неактивных пользователей",
                    {"deleted_count": deleted_count}
                )
                logger.info(f"🗑️ Удалено {deleted_count} неактивных пользователей")
                
        except Exception as e:
            logger.error(f"Ошибка очистки неактивных пользователей: {e}")
    
    async def _sync_with_remnawave(self, db: AsyncSession):
        try:
            now = datetime.utcnow()
            if (
                self._last_remnawave_sync_at
                and now - self._last_remnawave_sync_at < timedelta(minutes=self.REMNAWAVE_SYNC_INTERVAL_MINUTES)
            ):
                return

            if self._remnawave_sync_lock.locked():
                logger.info("⏭️ Batch sync VPN connection flags already running, skipping this cycle")
                return
            
            if not self.subscription_service.is_configured:
                logger.warning("RemnaWave API не настроен. Пропускаем синхронизацию")
                return

            async with self._remnawave_sync_lock:
                # Отметку ставим независимо от исхода: скан панели стоит ~240
                # запросов, и при поломке его нельзя гонять каждым циклом.
                self._last_remnawave_sync_at = now
                sync_stats = await RemnaWaveService().sync_vpn_connection_flags_from_panel(db)

                await self._log_monitoring_event(
                    db,
                    "remnawave_sync",
                    "Batch-синхронизация флагов подключения VPN с RemnaWave завершена",
                    sync_stats,
                )
                logger.info("🔄 Batch sync VPN connection flags completed: %s", sync_stats)
                
        except Exception as e:
            logger.error(f"Ошибка синхронизации с RemnaWave: {e}")
            await self._log_monitoring_event(
                db, "remnawave_sync_error",
                f"Ошибка синхронизации с RemnaWave: {str(e)}",
                {"error": str(e)},
                is_success=False
            )

    async def _collect_daily_subscription_metrics(self, db: AsyncSession):
        try:
            result = await self.daily_subscription_metrics_service.collect_for_yesterday(db)

            if result.get("skipped"):
                reason = result.get("reason")
                if reason == "already_collected":
                    logger.debug(
                        "Snapshot метрик подписок за %s уже собран, пропускаем",
                        result.get("date"),
                    )
                    return

                await self._log_monitoring_event(
                    db,
                    "daily_subscription_metrics_snapshot_skipped",
                    f"Сбор snapshot-метрик подписок пропущен: {reason}",
                    result,
                    is_success=False,
                )
                return

            await self._log_monitoring_event(
                db,
                "daily_subscription_metrics_snapshot",
                (
                    f"Собран snapshot метрик подписок за {result.get('date')}: "
                    f"платные={result.get('paid_users_count')}, "
                    f"потерянные={result.get('lost_paid_users_count')}"
                ),
                result,
            )
        except Exception as e:
            logger.error(f"Ошибка сбора snapshot-метрик подписок: {e}")
            await self._log_monitoring_event(
                db,
                "daily_subscription_metrics_snapshot_error",
                f"Ошибка сбора snapshot-метрик подписок: {str(e)}",
                {"error": str(e)},
                is_success=False,
            )

    async def _collect_user_daily_metrics(self, db: AsyncSession):
        try:
            result = await self.user_daily_metrics_service.collect_missing_recent_days(db)

            if result.get("skipped"):
                reason = result.get("reason")
                if reason == "already_collected":
                    logger.debug(
                        "Snapshot метрик пользователей за %s уже собран, пропускаем",
                        result.get("date"),
                    )
                    return

                await self._log_monitoring_event(
                    db,
                    "user_daily_metrics_snapshot_skipped",
                    f"Сбор snapshot-метрик пользователей пропущен: {reason}",
                    result,
                    is_success=False,
                )
                return

            await self._log_monitoring_event(
                db,
                "user_daily_metrics_snapshot",
                (
                    f"Проверены snapshot метрик пользователей за {result.get('range_start')}..{result.get('range_end')}: "
                    f"создано={result.get('created')}, "
                    f"пропущено={result.get('skipped_existing')}, "
                    f"ошибок={result.get('failed')}"
                ),
                result,
                is_success=(result.get("failed", 0) == 0),
            )
        except Exception as e:
            logger.error(f"Ошибка сбора snapshot-метрик пользователей: {e}")
            await self._log_monitoring_event(
                db,
                "user_daily_metrics_snapshot_error",
                f"Ошибка сбора snapshot-метрик пользователей: {str(e)}",
                {"error": str(e)},
                is_success=False,
            )

    async def _collect_trial_expiry_daily_metrics(self, db: AsyncSession):
        try:
            result = await self.trial_expiry_daily_metrics_service.collect_missing_ready_cohorts(db)

            if result.get("skipped"):
                reason = result.get("reason")
                if reason == "already_collected":
                    logger.debug(
                        "Snapshot конверсии истёкших триалов за %s уже собран, пропускаем",
                        result.get("date"),
                    )
                    return

                await self._log_monitoring_event(
                    db,
                    "trial_expiry_daily_metrics_snapshot_skipped",
                    f"Сбор конверсии истёкших триалов пропущен: {reason}",
                    result,
                    is_success=False,
                )
                return

            await self._log_monitoring_event(
                db,
                "trial_expiry_daily_metrics_snapshot",
                (
                    f"Проверены snapshot конверсии истёкших триалов за "
                    f"{result.get('range_start')}..{result.get('range_end')}: "
                    f"создано={result.get('created')}, "
                    f"пропущено={result.get('skipped_existing')}, "
                    f"ошибок={result.get('failed')}"
                ),
                result,
                is_success=(result.get("failed", 0) == 0),
            )
        except Exception as e:
            logger.error(f"Ошибка сбора конверсии истёкших триалов: {e}")
            await self._log_monitoring_event(
                db,
                "trial_expiry_daily_metrics_snapshot_error",
                f"Ошибка сбора конверсии истёкших триалов: {str(e)}",
                {"error": str(e)},
                is_success=False,
            )

    async def _collect_user_daily_traffic_usage(self, db: AsyncSession):
        try:
            result = await self.user_daily_traffic_usage_service.collect_for_yesterday(db)

            if result.get("skipped"):
                reason = result.get("reason")
                if reason == "already_collected":
                    logger.debug(
                        "Дневной трафик за %s уже собран, пропускаем",
                        result.get("date"),
                    )
                    return

                await self._log_monitoring_event(
                    db,
                    "user_daily_traffic_snapshot_skipped",
                    f"Сбор дневного трафика пропущен: {reason}",
                    result,
                    is_success=False,
                )
                return

            await self._log_monitoring_event(
                db,
                "user_daily_traffic_snapshot",
                f"Собран дневной трафик за {result.get('date')}: обработано {result.get('processed')} пользователей",
                result,
                is_success=(result.get("failed", 0) == 0),
            )
        except Exception as e:
            logger.error(f"Ошибка сбора дневного трафика пользователей: {e}")
            await self._log_monitoring_event(
                db,
                "user_daily_traffic_snapshot_error",
                f"Ошибка сбора дневного трафика пользователей: {str(e)}",
                {"error": str(e)},
                is_success=False,
            )

    async def _process_android_rate_requests(self, db: AsyncSession):
        try:
            result = await android_rate_request_service.process_due_requests(db, self.bot)
            if result.get("skipped"):
                logger.debug(
                    "Android rate request processing skipped: %s",
                    result.get("reason"),
                )
                return

            if result.get("sent") or result.get("unreachable") or result.get("failed"):
                await self._log_monitoring_event(
                    db,
                    "android_rate_requests_processed",
                    "Обработаны запросы оценки Android-приложения",
                    result,
                    is_success=(result.get("failed", 0) == 0),
                )
        except Exception as e:
            logger.error(f"Ошибка обработки запросов оценки Android-приложения: {e}")
            await self._log_monitoring_event(
                db,
                "android_rate_requests_error",
                f"Ошибка обработки запросов оценки Android-приложения: {str(e)}",
                {"error": str(e)},
                is_success=False,
            )

    async def _process_expired_subscription_feedbacks(self, db: AsyncSession):
        try:
            result = await expired_subscription_feedback_service.process_due_feedbacks(
                db,
                self.bot,
            )
            if result.get("skipped"):
                logger.debug(
                    "Expired subscription feedback processing skipped: %s",
                    result.get("reason"),
                )
                return

            if result.get("sent") or result.get("unreachable") or result.get("failed"):
                await self._log_monitoring_event(
                    db,
                    "expired_subscription_feedbacks_processed",
                    "Обработаны feedback-опросы по истекшим подпискам",
                    result,
                    is_success=(result.get("failed", 0) == 0),
                )
        except Exception as e:
            logger.error(f"Ошибка обработки feedback-опросов по истекшим подпискам: {e}")
            await self._log_monitoring_event(
                db,
                "expired_subscription_feedbacks_error",
                f"Ошибка обработки feedback-опросов по истекшим подпискам: {str(e)}",
                {"error": str(e)},
                is_success=False,
            )

    async def _check_ticket_sla(self, db: AsyncSession):
        try:
            # Quick guards
            # Allow runtime toggle from SupportSettingsService
            try:
                from app.services.support_settings_service import SupportSettingsService
                sla_enabled_runtime = SupportSettingsService.get_sla_enabled()
            except Exception:
                sla_enabled_runtime = getattr(settings, 'SUPPORT_TICKET_SLA_ENABLED', True)
            if not sla_enabled_runtime:
                return
            if not self.bot:
                return
            if not settings.is_admin_notifications_enabled():
                return

            from datetime import datetime, timedelta
            try:
                from app.services.support_settings_service import SupportSettingsService
                sla_minutes = max(1, int(SupportSettingsService.get_sla_minutes()))
            except Exception:
                sla_minutes = max(1, int(getattr(settings, 'SUPPORT_TICKET_SLA_MINUTES', 5)))
            cooldown_minutes = max(1, int(getattr(settings, 'SUPPORT_TICKET_SLA_REMINDER_COOLDOWN_MINUTES', 15)))
            now = datetime.utcnow()
            stale_before = now - timedelta(minutes=sla_minutes)
            cooldown_before = now - timedelta(minutes=cooldown_minutes)

            # Tickets to remind: open, no admin reply yet after user's last message (status OPEN), stale by SLA,
            # and either never reminded or cooldown passed
            result = await db.execute(
                select(Ticket)
                .options(selectinload(Ticket.user))
                .where(
                    and_(
                        Ticket.status == TicketStatus.OPEN.value,
                        Ticket.updated_at <= stale_before,
                        or_(Ticket.last_sla_reminder_at.is_(None), Ticket.last_sla_reminder_at <= cooldown_before),
                    )
                )
            )
            tickets = result.scalars().all()
            if not tickets:
                return

            from app.services.admin_notification_service import AdminNotificationService

            reminders_sent = 0
            service = AdminNotificationService(self.bot)

            for ticket in tickets:
                try:
                    waited_minutes = max(0, int((now - ticket.updated_at).total_seconds() // 60))
                    title = (ticket.title or '').strip()
                    if len(title) > 60:
                        title = title[:57] + '...'

                    # Детали пользователя: имя, Telegram ID и username
                    full_name = ticket.user.full_name if ticket.user else "Unknown"
                    telegram_id_display = ticket.user.telegram_id if ticket.user else "—"
                    username_display = (ticket.user.username or "отсутствует") if ticket.user else "отсутствует"

                    text = (
                        f"⏰ <b>Ожидание ответа на тикет превышено</b>\n\n"
                        f"🆔 <b>ID:</b> <code>{ticket.id}</code>\n"
                        f"👤 <b>Пользователь:</b> {full_name}\n"
                        f"🆔 <b>Telegram ID:</b> <code>{telegram_id_display}</code>\n"
                        f"📱 <b>Username:</b> @{username_display}\n"
                        f"📝 <b>Заголовок:</b> {title or '—'}\n"
                        f"⏱️ <b>Ожидает ответа:</b> {waited_minutes} мин\n"
                    )

                    sent = await service.send_ticket_event_notification(text)
                    if sent:
                        ticket.last_sla_reminder_at = now
                        reminders_sent += 1
                        # commit after each to persist timestamp and avoid duplicate reminders on crash
                        await db.commit()
                except Exception as notify_error:
                    logger.error(f"Ошибка отправки SLA-уведомления по тикету {ticket.id}: {notify_error}")

            if reminders_sent > 0:
                await self._log_monitoring_event(
                    db,
                    "ticket_sla_reminders_sent",
                    f"Отправлено {reminders_sent} SLA-напоминаний по тикетам",
                    {"count": reminders_sent},
                )
        except Exception as e:
            logger.error(f"Ошибка проверки SLA тикетов: {e}")

    async def _sla_loop(self):
        try:
            interval_seconds = max(10, int(getattr(settings, 'SUPPORT_TICKET_SLA_CHECK_INTERVAL_SECONDS', 60)))
        except Exception:
            interval_seconds = 60
        while self.is_running:
            try:
                async for db in get_db():
                    try:
                        await self._check_ticket_sla(db)
                    finally:
                        break
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Ошибка в SLA-цикле: {e}")
            await asyncio.sleep(interval_seconds)

    async def _log_monitoring_event(
        self,
        db: AsyncSession,
        event_type: str,
        message: str,
        data: Dict[str, Any] = None,
        is_success: bool = True
    ):
        try:
            log_entry = MonitoringLog(
                event_type=event_type,
                message=message,
                data=data or {},
                is_success=is_success
            )
            
            db.add(log_entry)
            await db.commit()
            
        except Exception as e:
            logger.error(f"Ошибка логирования события мониторинга: {e}")

    async def get_monitoring_status(self, db: AsyncSession) -> Dict[str, Any]:
        try:
            from sqlalchemy import select, desc
            
            recent_events_result = await db.execute(
                select(MonitoringLog)
                .order_by(desc(MonitoringLog.created_at))
                .limit(10)
            )
            recent_events = recent_events_result.scalars().all()
            
            yesterday = datetime.utcnow() - timedelta(days=1)
            
            events_24h_result = await db.execute(
                select(MonitoringLog)
                .where(MonitoringLog.created_at >= yesterday)
            )
            events_24h = events_24h_result.scalars().all()
            
            successful_events = sum(1 for event in events_24h if event.is_success)
            failed_events = sum(1 for event in events_24h if not event.is_success)
            
            return {
                "is_running": self.is_running,
                "last_update": datetime.utcnow(),
                "recent_events": [
                    {
                        "type": event.event_type,
                        "message": event.message,
                        "success": event.is_success,
                        "created_at": event.created_at
                    }
                    for event in recent_events
                ],
                "stats_24h": {
                    "total_events": len(events_24h),
                    "successful": successful_events,
                    "failed": failed_events,
                    "success_rate": round(successful_events / len(events_24h) * 100, 1) if events_24h else 0
                }
            }
            
        except Exception as e:
            logger.error(f"Ошибка получения статуса мониторинга: {e}")
            return {
                "is_running": self.is_running,
                "last_update": datetime.utcnow(),
                "recent_events": [],
                "stats_24h": {
                    "total_events": 0,
                    "successful": 0,
                    "failed": 0,
                    "success_rate": 0
                }
            }
    
    async def force_check_subscriptions(self, db: AsyncSession) -> Dict[str, int]:
        try:
            expired_subscriptions = await get_expired_subscriptions(db)
            expired_count = 0
            
            for subscription in expired_subscriptions:
                await deactivate_subscription(db, subscription)
                expired_count += 1
            
            expiring_subscriptions = await get_expiring_subscriptions(db, 1)
            expiring_count = len(expiring_subscriptions)
            
            autopay_subscriptions = await get_subscriptions_for_autopay(db)
            autopay_processed = 0
            
            for subscription in autopay_subscriptions:
                user = await get_user_by_id(db, subscription.user_id)
                if user and user.balance_kopeks >= settings.PRICE_30_DAYS:
                    autopay_processed += 1
            
            await self._log_monitoring_event(
                db, "manual_check_subscriptions",
                f"Принудительная проверка: истекло {expired_count}, истекает {expiring_count}, автоплатежей {autopay_processed}",
                {
                    "expired": expired_count,
                    "expiring": expiring_count,
                    "autopay_ready": autopay_processed
                }
            )
            
            return {
                "expired": expired_count,
                "expiring": expiring_count,
                "autopay_ready": autopay_processed
            }
            
        except Exception as e:
            logger.error(f"Ошибка принудительной проверки подписок: {e}")
            return {"expired": 0, "expiring": 0, "autopay_ready": 0}
    
    async def get_monitoring_logs(
        self,
        db: AsyncSession,
        limit: int = 50,
        event_type: Optional[str] = None,
        page: int = 1,
        per_page: int = 20
    ) -> List[Dict[str, Any]]:
        try:
            from sqlalchemy import select, desc
            
            query = select(MonitoringLog).order_by(desc(MonitoringLog.created_at))
            
            if event_type:
                query = query.where(MonitoringLog.event_type == event_type)
            
            if page > 1 or per_page != 20:
                offset = (page - 1) * per_page
                query = query.offset(offset).limit(per_page)
            else:
                query = query.limit(limit)
            
            result = await db.execute(query)
            logs = result.scalars().all()
            
            return [
                {
                    "id": log.id,
                    "event_type": log.event_type,
                    "message": log.message,
                    "data": log.data,
                    "is_success": log.is_success,
                    "created_at": log.created_at
                }
                for log in logs
            ]
            
        except Exception as e:
            logger.error(f"Ошибка получения логов мониторинга: {e}")
            return []

    async def get_monitoring_logs_count(
        self,
        db: AsyncSession,
        event_type: Optional[str] = None
    ) -> int:
        try:
            from sqlalchemy import select, func

            query = select(func.count(MonitoringLog.id))

            if event_type:
                query = query.where(MonitoringLog.event_type == event_type)

            result = await db.execute(query)
            count = result.scalar()

            return count or 0

        except Exception as e:
            logger.error(f"Ошибка получения количества логов: {e}")
            return 0

    async def get_monitoring_event_types(self, db: AsyncSession) -> List[str]:
        try:
            from sqlalchemy import select

            result = await db.execute(
                select(MonitoringLog.event_type)
                .where(MonitoringLog.event_type.isnot(None))
                .distinct()
                .order_by(MonitoringLog.event_type)
            )

            return [row[0] for row in result.fetchall() if row[0]]

        except Exception as e:
            logger.error(f"Ошибка получения списка типов событий мониторинга: {e}")
            return []
    
    async def cleanup_old_logs(self, db: AsyncSession, days: int = 30) -> int:
        try:
            from sqlalchemy import delete, select
            
            if days == 0:
                result = await db.execute(delete(MonitoringLog))
            else:
                cutoff_date = datetime.utcnow() - timedelta(days=days)
                result = await db.execute(
                    delete(MonitoringLog).where(MonitoringLog.created_at < cutoff_date)
                )
            
            deleted_count = result.rowcount
            await db.commit()
            
            if days == 0:
                logger.info(f"🗑️ Удалены все логи мониторинга ({deleted_count} записей)")
            else:
                logger.info(f"🗑️ Удалено {deleted_count} старых записей логов (старше {days} дней)")
                
            return deleted_count
            
        except Exception as e:
            logger.error(f"Ошибка очистки логов: {e}")
            await db.rollback()
            return 0


monitoring_service = MonitoringService()
