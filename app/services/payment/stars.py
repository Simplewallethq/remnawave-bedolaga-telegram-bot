"""Логика Telegram Stars вынесена в отдельный mixin.

Методы здесь отвечают только за работу с звёздами, что позволяет держать
основной сервис компактным и облегчает тестирование конкретных сценариев.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
from typing import Optional

from aiogram.types import LabeledPrice
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.transaction import create_transaction
from app.database.crud.subscription_event import record_subscription_purchase_event
from app.database.crud.user import get_user_by_id
from app.database.models import PaymentMethod, TransactionType
from app.external.telegram_stars import TelegramStarsService
from app.services.subscription_auto_purchase_service import (
    auto_purchase_saved_cart_after_topup,
)
from app.utils.success_notifications import (
    build_success_management_keyboard,
    format_subscription_purchase_success,
    subscription_plan_name,
)
from app.utils.user_utils import format_referrer_info

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _SimpleSubscriptionPayload:
    """Данные для простой подписки, извлечённые из payload звёздного платежа."""

    subscription_id: Optional[int]
    period_days: Optional[int]


class TelegramStarsMixin:
    """Mixin с операциями создания и обработки платежей через Telegram Stars."""

    async def create_stars_invoice(
        self,
        amount_kopeks: int,
        description: str,
        payload: Optional[str] = None,
        *,
        stars_amount: Optional[int] = None,
    ) -> str:
        """Создаёт invoice в Telegram Stars, автоматически рассчитывая количество звёзд."""
        if not self.bot or not getattr(self, "stars_service", None):
            raise ValueError("Bot instance required for Stars payments")

        try:
            amount_rubles = Decimal(amount_kopeks) / Decimal(100)

            # Если количество звёзд не задано, вычисляем его из курса.
            if stars_amount is None:
                if settings.get_stars_rate() <= 0:
                    raise ValueError("Stars rate must be positive")
                stars_amount = settings.rubles_to_stars(float(amount_rubles))

            if stars_amount <= 0:
                raise ValueError("Stars amount must be positive")

            invoice_link = await self.bot.create_invoice_link(
                title="Пополнение баланса VPN",
                description=f"{description} (≈{stars_amount} ⭐)",
                payload=payload or f"balance_topup_{amount_kopeks}",
                provider_token="",
                currency="XTR",
                prices=[LabeledPrice(label="Пополнение", amount=stars_amount)],
            )

            logger.info(
                "Создан Stars invoice на %s звезд (~%s)",
                stars_amount,
                settings.format_price(amount_kopeks),
            )
            return invoice_link

        except Exception as error:
            logger.error("Ошибка создания Stars invoice: %s", error)
            raise

    async def process_stars_payment(
        self,
        db: AsyncSession,
        user_id: int,
        stars_amount: int,
        payload: str,
        telegram_payment_charge_id: str,
    ) -> bool:
        """Финализирует платеж, пришедший из Telegram Stars, и обновляет баланс пользователя."""
        try:
            rubles_amount = TelegramStarsService.calculate_rubles_from_stars(
                stars_amount
            )
            amount_kopeks = int(
                (rubles_amount * Decimal(100)).to_integral_value(
                    rounding=ROUND_HALF_UP
                )
            )

            simple_payload = self._parse_simple_subscription_payload(
                payload,
                user_id,
            )

            transaction_description = (
                f"Оплата подписки через Telegram Stars ({stars_amount} ⭐)"
                if simple_payload
                else f"Пополнение через Telegram Stars ({stars_amount} ⭐)"
            )
            transaction_type = (
                TransactionType.SUBSCRIPTION_PAYMENT
                if simple_payload
                else TransactionType.DEPOSIT
            )

            transaction = await create_transaction(
                db=db,
                user_id=user_id,
                type=transaction_type,
                amount_kopeks=amount_kopeks,
                description=transaction_description,
                payment_method=PaymentMethod.TELEGRAM_STARS,
                external_id=telegram_payment_charge_id,
                is_completed=True,
            )

            user = await get_user_by_id(db, user_id)
            if not user:
                logger.error(
                    "Пользователь с ID %s не найден при обработке Stars платежа",
                    user_id,
                )
                return False

            if simple_payload:
                return await self._finalize_simple_subscription_stars_payment(
                    db=db,
                    user=user,
                    transaction=transaction,
                    amount_kopeks=amount_kopeks,
                    stars_amount=stars_amount,
                    payload_data=simple_payload,
                    telegram_payment_charge_id=telegram_payment_charge_id,
                )

            # Токен snapshot'а частичной оплаты тарифа — хвост "_ts{token}" в payload.
            checkout_snapshot = None
            if payload and "_ts" in payload:
                from app.services.tariff_partial_payment_service import (
                    pop_snapshot_for_stars,
                )

                token = payload.rsplit("_ts", 1)[-1]
                checkout_snapshot = await pop_snapshot_for_stars(user_id, token)

            return await self._finalize_stars_balance_topup(
                db=db,
                user=user,
                transaction=transaction,
                amount_kopeks=amount_kopeks,
                stars_amount=stars_amount,
                telegram_payment_charge_id=telegram_payment_charge_id,
                checkout_snapshot=checkout_snapshot,
            )

        except Exception as error:
            logger.error("Ошибка обработки Stars платежа: %s", error, exc_info=True)
            return False

    @staticmethod
    def _parse_simple_subscription_payload(
        payload: str,
        expected_user_id: int,
    ) -> Optional[_SimpleSubscriptionPayload]:
        """Пытается извлечь параметры простой подписки из payload звёздного платежа."""

        prefix = "simple_sub_"
        if not payload or not payload.startswith(prefix):
            return None

        tail = payload[len(prefix) :]
        parts = tail.split("_", 2)
        if len(parts) < 3:
            logger.warning(
                "Payload Stars simple subscription имеет некорректный формат: %s",
                payload,
            )
            return None

        user_part, subscription_part, period_part = parts

        try:
            payload_user_id = int(user_part)
        except ValueError:
            logger.warning(
                "Не удалось разобрать user_id в payload Stars simple subscription: %s",
                payload,
            )
            return None

        if payload_user_id != expected_user_id:
            logger.warning(
                "Получен payload Stars simple subscription с чужим user_id: %s (ожидался %s)",
                payload_user_id,
                expected_user_id,
            )
            return None

        try:
            subscription_id = int(subscription_part)
        except ValueError:
            logger.warning(
                "Не удалось разобрать subscription_id в payload Stars simple subscription: %s",
                payload,
            )
            return None

        period_days: Optional[int] = None
        try:
            period_days = int(period_part)
        except ValueError:
            logger.warning(
                "Не удалось разобрать период в payload Stars simple subscription: %s",
                payload,
            )

        return _SimpleSubscriptionPayload(
            subscription_id=subscription_id,
            period_days=period_days,
        )

    async def _finalize_simple_subscription_stars_payment(
        self,
        db: AsyncSession,
        user,
        transaction,
        amount_kopeks: int,
        stars_amount: int,
        payload_data: _SimpleSubscriptionPayload,
        telegram_payment_charge_id: str,
    ) -> bool:
        """Активация простой подписки, оплаченной через Telegram Stars."""

        period_days = payload_data.period_days or settings.SIMPLE_SUBSCRIPTION_PERIOD_DAYS
        pending_subscription = None

        if payload_data.subscription_id is not None:
            try:
                from sqlalchemy import select
                from app.database.models import Subscription

                result = await db.execute(
                    select(Subscription).where(
                        Subscription.id == payload_data.subscription_id,
                        Subscription.user_id == user.id,
                    )
                )
                pending_subscription = result.scalar_one_or_none()
            except Exception as lookup_error:  # pragma: no cover - диагностический лог
                logger.error(
                    "Ошибка поиска pending подписки %s для пользователя %s: %s",
                    payload_data.subscription_id,
                    user.id,
                    lookup_error,
                    exc_info=True,
                )
                pending_subscription = None

            if not pending_subscription:
                logger.error(
                    "Не найдена pending подписка %s для пользователя %s",
                    payload_data.subscription_id,
                    user.id,
                )
                return False

            if payload_data.period_days is None:
                start_point = pending_subscription.start_date or datetime.utcnow()
                end_point = pending_subscription.end_date or start_point
                computed_days = max(1, (end_point - start_point).days or 0)
                period_days = max(period_days, computed_days)

        try:
            from app.database.crud.subscription import activate_pending_subscription

            subscription = await activate_pending_subscription(
                db=db,
                user_id=user.id,
                period_days=period_days,
            )
        except Exception as error:
            logger.error(
                "Ошибка активации pending подписки для пользователя %s: %s",
                user.id,
                error,
                exc_info=True,
            )
            return False

        if not subscription:
            logger.error(
                "Не удалось активировать pending подписку пользователя %s",
                user.id,
            )
            return False

        try:
            from app.services.subscription_service import SubscriptionService

            subscription_service = SubscriptionService()
            remnawave_user = await subscription_service.create_remnawave_user(
                db,
                subscription,
            )
            if remnawave_user:
                await db.refresh(subscription)
        except Exception as sync_error:  # pragma: no cover - диагностический лог
            logger.error(
                "Ошибка синхронизации подписки с RemnaWave для пользователя %s: %s",
                user.id,
                sync_error,
                exc_info=True,
            )

        period_display = period_days
        if not period_display and getattr(subscription, "start_date", None) and getattr(
            subscription, "end_date", None
        ):
            period_display = max(1, (subscription.end_date - subscription.start_date).days or 0)
        if not period_display:
            period_display = settings.SIMPLE_SUBSCRIPTION_PERIOD_DAYS

        await record_subscription_purchase_event(
            db,
            user_id=user.id,
            subscription_id=subscription.id,
            transaction_id=getattr(transaction, "id", None),
            amount_kopeks=amount_kopeks,
            occurred_at=(
                getattr(transaction, "completed_at", None)
                or getattr(transaction, "created_at", None)
            ),
            period_days=period_display,
            was_trial_conversion=False,
            payment_method=getattr(transaction, "payment_method", PaymentMethod.TELEGRAM_STARS),
            source="stars_simple_subscription",
            starts_at=subscription.start_date,
            ends_at=subscription.end_date,
        )

        if getattr(self, "bot", None):
            try:
                await self.bot.send_message(
                    chat_id=user.telegram_id,
                    text=format_subscription_purchase_success(
                        plan=subscription_plan_name(subscription),
                        period=period_display,
                        end_date=subscription.end_date,
                    ),
                    reply_markup=build_success_management_keyboard(),
                    parse_mode="HTML",
                )
                logger.info(
                    "✅ Пользователь %s получил уведомление об оплате подписки через Stars",
                    user.telegram_id,
                )
            except Exception as error:  # pragma: no cover - диагностический лог
                logger.error(
                    "Ошибка отправки уведомления о подписке через Stars: %s",
                    error,
                    exc_info=True,
                )

        if getattr(self, "bot", None):
            try:
                from app.services.admin_notification_service import AdminNotificationService

                notification_service = AdminNotificationService(self.bot)
                try:
                    await notification_service.send_subscription_purchase_notification(
                        db,
                        user,
                        subscription,
                        transaction,
                        period_display,
                        was_trial_conversion=False,
                        record_event=False,
                    )
                except TypeError as type_error:
                    if "record_event" not in str(type_error):
                        raise
                    await notification_service.send_subscription_purchase_notification(
                        db,
                        user,
                        subscription,
                        transaction,
                        period_display,
                        was_trial_conversion=False,
                    )
            except Exception as admin_error:  # pragma: no cover - диагностический лог
                logger.error(
                    "Ошибка уведомления администраторов о подписке через Stars: %s",
                    admin_error,
                    exc_info=True,
                )

        logger.info(
            "✅ Обработан Stars платеж как покупка подписки: пользователь %s, %s звезд → %s",
            user.id,
            stars_amount,
            settings.format_price(amount_kopeks),
        )
        return True

    async def _finalize_stars_balance_topup(
        self,
        db: AsyncSession,
        user,
        transaction,
        amount_kopeks: int,
        stars_amount: int,
        telegram_payment_charge_id: str,
        checkout_snapshot: Optional[dict] = None,
    ) -> bool:
        """Начисляет баланс пользователю после оплаты Stars и запускает автопокупку."""

        # Запоминаем старые значения, чтобы корректно построить уведомления.
        old_balance = user.balance_kopeks
        was_first_topup = not user.has_made_first_topup

        # Обновляем баланс в БД.
        user.balance_kopeks += amount_kopeks
        user.updated_at = datetime.utcnow()

        promo_group = user.get_primary_promo_group()
        subscription = getattr(user, "subscription", None)
        referrer_info = format_referrer_info(user)
        topup_status = "🆕 Первое пополнение" if was_first_topup else "🔄 Пополнение"

        await db.commit()

        description_for_referral = (
            f"Пополнение Stars: {settings.format_price(amount_kopeks)} ({stars_amount} ⭐)"
        )
        logger.info(
            "🔍 Проверка реферальной логики для описания: '%s'",
            description_for_referral,
        )

        lower_description = description_for_referral.lower()
        contains_allowed_keywords = any(
            word in lower_description for word in ["пополнение", "stars", "yookassa", "topup"]
        )
        contains_forbidden_keywords = any(
            word in lower_description for word in ["комиссия", "бонус"]
        )
        allow_referral = contains_allowed_keywords and not contains_forbidden_keywords

        if allow_referral:
            logger.info(
                "🔞 Вызов process_referral_topup для пользователя %s",
                user.id,
            )
            try:
                from app.services.referral_service import process_referral_topup

                await process_referral_topup(
                    db,
                    user.id,
                    amount_kopeks,
                    getattr(self, "bot", None),
                )
            except Exception as error:  # pragma: no cover - диагностический лог
                logger.error(
                    "Ошибка обработки реферального пополнения: %s",
                    error,
                )
        else:
            logger.info(
                "❌ Описание '%s' не подходит для реферальной логики",
                description_for_referral,
            )

        if was_first_topup and not user.has_made_first_topup:
            user.has_made_first_topup = True
            await db.commit()

        await db.refresh(user)

        logger.info(
            "💰 Баланс пользователя %s изменен: %s → %s (Δ +%s)",
            user.telegram_id,
            old_balance,
            user.balance_kopeks,
            amount_kopeks,
        )

        if getattr(self, "bot", None):
            try:
                from app.services.admin_notification_service import AdminNotificationService

                notification_service = AdminNotificationService(self.bot)
                await notification_service.send_balance_topup_notification(
                    user,
                    transaction,
                    old_balance,
                    topup_status=topup_status,
                    referrer_info=referrer_info,
                    subscription=subscription,
                    promo_group=promo_group,
                    db=db,
                )
            except Exception as error:  # pragma: no cover - диагностический лог
                logger.error(
                    "Ошибка отправки уведомления о пополнении Stars: %s",
                    error,
                    exc_info=True,
                )

        # Сначала пробуем автопокупку — если успех, не отправляем generic уведомление
        auto_purchase_success = False
        try:
            from app.services.user_cart_service import user_cart_service

            has_saved_cart = await user_cart_service.has_user_cart(user.id)
            if has_saved_cart or checkout_snapshot:
                try:
                    auto_purchase_success = await auto_purchase_saved_cart_after_topup(
                        db,
                        user,
                        bot=getattr(self, "bot", None),
                        checkout_snapshot=checkout_snapshot,
                    )
                except Exception as auto_error:  # pragma: no cover - диагностический лог
                    logger.error(
                        "Ошибка автоматической покупки подписки для пользователя %s: %s",
                        user.id,
                        auto_error,
                        exc_info=True,
                    )

                if auto_purchase_success:
                    has_saved_cart = False
        except Exception as error:  # pragma: no cover - диагностический лог
            logger.error(
                "Ошибка при работе с сохраненной корзиной для пользователя %s: %s",
                user.id,
                error,
                exc_info=True,
            )

        logger.info(
            "✅ Обработан Stars платеж: пользователь %s, %s звезд → %s",
            user.id,
            stars_amount,
            settings.format_price(amount_kopeks),
        )
        self._last_auto_purchase_success = auto_purchase_success
        return True
