"""Общие инструменты платёжного сервиса.

В этом модуле собраны методы, которые нужны всем платёжным каналам:
построение клавиатур, базовые уведомления и стандартная обработка
успешных платежей.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.exc import MissingGreenlet
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.user import get_user_by_telegram_id
from app.database.database import get_db
from app.localization.texts import get_texts
from app.services.subscription_checkout_service import (
    has_subscription_checkout_draft,
    should_offer_checkout_resume,
)
from app.services.user_cart_service import user_cart_service
from app.utils.miniapp_buttons import build_miniapp_or_callback_button

logger = logging.getLogger(__name__)


class PaymentCommonMixin:
    """Mixin с базовой логикой, которую используют остальные платёжные блоки."""

    async def build_topup_success_keyboard(self, user: Any) -> InlineKeyboardMarkup:
        """Формирует клавиатуру по завершении платежа, подстраиваясь под пользователя."""
        # Загружаем нужные тексты с учётом выбранного языка пользователя.
        texts = get_texts(user.language if user else "ru")

        # Определяем статус подписки, чтобы показать подходящую кнопку.
        has_active_subscription = False
        subscription = None
        if user:
            try:
                subscription = user.subscription
                has_active_subscription = bool(
                    subscription
                    and not getattr(subscription, "is_trial", False)
                    and getattr(subscription, "is_active", False)
                )
            except MissingGreenlet as error:
                logger.warning(
                    "Не удалось лениво загрузить подписку пользователя %s при построении клавиатуры после пополнения: %s",
                    getattr(user, "id", None),
                    error,
                )
            except Exception as error:  # pragma: no cover - защитный код
                logger.error(
                    "Ошибка загрузки подписки пользователя %s при построении клавиатуры после пополнения: %s",
                    getattr(user, "id", None),
                    error,
                )

        # Создаем основную кнопку: если есть активная подписка - продлить, иначе купить
        first_button = build_miniapp_or_callback_button(
            text=(
                texts.MENU_EXTEND_SUBSCRIPTION
                if has_active_subscription
                else texts.MENU_BUY_SUBSCRIPTION
            ),
            callback_data=(
                "subscription_extend" if has_active_subscription else "menu_buy"
            ),
        )

        # Кнопка активации подписки (всегда отображается)
        activate_subscription_button = build_miniapp_or_callback_button(
            text="🚀 Активировать подписку",
            callback_data="menu_buy"  # Используем ту же callback_data что и "Купить подписку"
        )

        keyboard_rows: list[list[InlineKeyboardButton]] = [
            [first_button],
            [activate_subscription_button]
        ]

        # Если для пользователя есть незавершённый checkout, предлагаем вернуться к нему.
        if user:
            try:
                has_saved_cart = await user_cart_service.has_user_cart(user.id)
            except Exception as cart_error:
                logger.warning(
                    "Не удалось проверить наличие сохраненной корзины у пользователя %s: %s",
                    user.id,
                    cart_error,
                )
                has_saved_cart = False

            if has_saved_cart:
                keyboard_rows.append([
                    build_miniapp_or_callback_button(
                        text=texts.RETURN_TO_SUBSCRIPTION_CHECKOUT,
                        callback_data="return_to_saved_cart",
                    )
                ])
            else:
                draft_exists = await has_subscription_checkout_draft(user.id)
                if should_offer_checkout_resume(user, draft_exists, subscription=subscription):
                    keyboard_rows.append([
                        build_miniapp_or_callback_button(
                            text=texts.RETURN_TO_SUBSCRIPTION_CHECKOUT,
                            callback_data="subscription_resume_checkout",
                        )
                    ])

        # Стандартные кнопки быстрого доступа к балансу и главному меню.
        keyboard_rows.append([
            build_miniapp_or_callback_button(
                text="💰 Мой баланс",
                callback_data="menu_balance",
            )
        ])
        keyboard_rows.append([
            InlineKeyboardButton(
                text="🏠 Главное меню",
                callback_data="back_to_menu",
            )
        ])

        return InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

    async def _send_payment_success_notification(
        self,
        telegram_id: int,
        amount_kopeks: int,
        user: Any | None = None,
        *,
        db: AsyncSession | None = None,
        payment_method_title: str | None = None,
    ) -> None:
        """Отправляет пользователю уведомление об успешном платеже."""
        if not telegram_id:
            # Веб-пользователь кабинета без Telegram — уведомление в боте пропускаем.
            return

        if not getattr(self, "bot", None):
            # Если бот не передан (например, внутри фоновых задач), уведомление пропускаем.
            return

        user_snapshot = await self._ensure_user_snapshot(
            telegram_id,
            user,
            db=db,
        )

        try:
            keyboard = await self.build_topup_success_keyboard(user_snapshot)

            payment_method = payment_method_title or "Банковская карта (YooKassa)"
            message = (
                "✅ <b>Платеж успешно завершен!</b>\n\n"
                f"💰 Сумма: {settings.format_price(amount_kopeks)}\n"
                f"💳 Способ: {payment_method}\n\n"
                "Средства зачислены на ваш баланс!\n\n"
                "⚠️ <b>Важно:</b> Пополнение баланса не активирует подписку автоматически. "
                "Обязательно активируйте подписку отдельно!\n\n"
                f"🔄 При наличии сохранённой корзины подписки и включенной автопокупке, "
                f"подписка будет приобретена автоматически после пополнения баланса."
            )

            await self.bot.send_message(
                chat_id=telegram_id,
                text=message,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        except Exception as error:
            logger.error(
                "Ошибка отправки уведомления пользователю %s: %s",
                telegram_id,
                error,
            )

    async def _ensure_user_snapshot(
        self,
        telegram_id: int,
        user: Any | None,
        *,
        db: AsyncSession | None = None,
    ) -> Any | None:
        """Гарантирует, что данные пользователя пригодны для построения клавиатуры."""

        def _build_snapshot(source: Any | None) -> SimpleNamespace | None:
            if source is None:
                return None

            subscription = getattr(source, "subscription", None)
            subscription_snapshot = None

            if subscription is not None:
                subscription_snapshot = SimpleNamespace(
                    is_trial=getattr(subscription, "is_trial", False),
                    is_active=getattr(subscription, "is_active", False),
                    actual_status=getattr(subscription, "actual_status", None),
                )

            return SimpleNamespace(
                id=getattr(source, "id", None),
                telegram_id=getattr(source, "telegram_id", None),
                language=getattr(source, "language", "ru"),
                subscription=subscription_snapshot,
            )

        try:
            snapshot = _build_snapshot(user)
        except MissingGreenlet:
            snapshot = None

        if snapshot is not None:
            return snapshot

        fetch_session = db

        if fetch_session is not None:
            try:
                fetched_user = await get_user_by_telegram_id(fetch_session, telegram_id)
                return _build_snapshot(fetched_user)
            except Exception as fetch_error:
                logger.warning(
                    "Не удалось обновить пользователя %s из переданной сессии: %s",
                    telegram_id,
                    fetch_error,
                )

        try:
            async for db_session in get_db():
                fetched_user = await get_user_by_telegram_id(db_session, telegram_id)
                return _build_snapshot(fetched_user)
        except Exception as fetch_error:
            logger.warning(
                "Не удалось получить пользователя %s для уведомления: %s",
                telegram_id,
                fetch_error,
            )

        return None

    async def process_successful_payment(
        self,
        payment_id: str,
        amount_kopeks: int,
        user_id: int,
        payment_method: str,
    ) -> bool:
        """Общая точка учёта успешных платежей (используется провайдерами при необходимости)."""
        try:
            logger.info(
                "Обработан успешный платеж: %s, %s₽, пользователь %s, метод %s",
                payment_id,
                amount_kopeks / 100,
                user_id,
                payment_method,
            )
            return True
        except Exception as error:
            logger.error("Ошибка обработки платежа %s: %s", payment_id, error)
            return False
